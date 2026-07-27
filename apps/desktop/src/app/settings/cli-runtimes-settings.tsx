import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  activateCliAccount,
  addCliAccount,
  type CliAccount,
  deleteCliAccount,
  getHermesConfigRecord,
  listCliAccounts,
  saveHermesConfig
} from '@/hermes'
import { Terminal, Trash2 } from '@/lib/icons'
import { notify } from '@/store/notifications'

import { getNested, setNested } from './helpers'
import { ListRow, SectionHeading, SettingsContent } from './primitives'

// Copy here is deliberately plain English literals rather than useI18n(), for
// the same reason as subagent-task.tsx: localising it would mean inventing
// translations for four locale files that this change cannot authentically
// produce. The nav label IS localised (settings.nav.providerCliRuntimes) since
// it is a short, standard technical term. Localising this body is a follow-up.

type RuntimeKey = 'model_anthropic_runtime' | 'model_openai_runtime'

interface RuntimeSpec {
  cli: string
  configKey: RuntimeKey
  description: string
  enabledValue: string
  label: string
  provider: CliAccount['provider']
}

const RUNTIMES: RuntimeSpec[] = [
  {
    cli: 'claude',
    configKey: 'model_anthropic_runtime',
    description:
      'Hand each Anthropic turn to the real claude CLI — its own tools, sub-agents and subscription/OAuth auth — instead of the Messages API. Only applies when the selected model resolves to the anthropic provider.',
    enabledValue: 'claude_code_sdk',
    label: 'Claude Code',
    provider: 'claude_code_sdk'
  },
  {
    cli: 'codex',
    configKey: 'model_openai_runtime',
    description:
      'Hand each OpenAI/Codex turn to a `codex app-server` subprocess, so terminal, file-ops and patching run inside Codex’s own runtime. Only applies when the selected model resolves to an OpenAI/Codex provider.',
    enabledValue: 'codex_app_server',
    label: 'Codex',
    provider: 'codex'
  }
]

type ClaudeCliKey = 'claude_code.binary_path' | 'claude_code.config_dir' | 'claude_code.extra_args'

const CLAUDE_CLI_FIELDS: {
  configKey: ClaudeCliKey
  description: string
  label: string
  placeholder: string
}[] = [
  {
    configKey: 'claude_code.binary_path',
    description: 'Path to the Claude binary used by this instance.',
    label: 'Binary path',
    placeholder: 'claude'
  },
  {
    configKey: 'claude_code.config_dir',
    description:
      'Custom Claude home and config directory. Keeps .claude.json and .claude separate. A CLI account selected with /cli-account overrides this for the current session.',
    label: 'CLAUDE_CONFIG_DIR path',
    placeholder: '~/.claude'
  },
  {
    configKey: 'claude_code.extra_args',
    description: 'Additional CLI arguments passed on session start.',
    label: 'Launch arguments',
    placeholder: 'e.g. --chrome'
  }
]

function providerLabel(provider: CliAccount['provider']): string {
  return provider === 'claude_code_sdk' ? 'Claude Code' : 'Codex'
}

export function CliRuntimesSettings({ onConfigSaved }: { onConfigSaved?: () => void }) {
  const [runtimes, setRuntimes] = useState<Record<string, string>>({})
  const [claudeCli, setClaudeCli] = useState<Record<string, string>>({})
  const [accounts, setAccounts] = useState<CliAccount[]>([])
  const [busy, setBusy] = useState(false)

  // Add-account form
  const [newName, setNewName] = useState('')
  const [newProvider, setNewProvider] = useState<CliAccount['provider']>('claude_code_sdk')
  const [newDir, setNewDir] = useState('')

  const refreshAccounts = useCallback(async () => {
    try {
      const { accounts: rows } = await listCliAccounts()
      setAccounts(rows)
    } catch {
      // Listing is best-effort — an unreachable backend just shows an empty table.
      setAccounts([])
    }
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        const config = await getHermesConfigRecord()
        setRuntimes({
          model_anthropic_runtime: String(config.model_anthropic_runtime ?? 'auto'),
          model_openai_runtime: String(config.model_openai_runtime ?? 'auto')
        })
        setClaudeCli(
          Object.fromEntries(
            CLAUDE_CLI_FIELDS.map(field => [field.configKey, String(getNested(config, field.configKey) ?? '')])
          )
        )
      } catch {
        setRuntimes({ model_anthropic_runtime: 'auto', model_openai_runtime: 'auto' })
      }

      await refreshAccounts()
    })()
  }, [refreshAccounts])

  const setRuntime = async (key: RuntimeKey, value: string) => {
    const previous = runtimes[key] ?? 'auto'
    setRuntimes(current => ({ ...current, [key]: value }))

    try {
      // Read-modify-write the whole record: the PUT deep-merges server-side,
      // so sending just this key would still be safe, but reusing the record
      // keeps the cached config in step with every other settings surface.
      const config = await getHermesConfigRecord()
      await saveHermesConfig({ ...config, [key]: value })
      onConfigSaved?.()
      notify({ kind: 'info', message: `Saved. Takes effect on the next turn.` })
    } catch (error) {
      setRuntimes(current => ({ ...current, [key]: previous }))
      notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save runtime' })
    }
  }

  const submitAccount = async () => {
    setBusy(true)

    try {
      const { probe } = await addCliAccount({
        config_dir: newDir.trim(),
        name: newName.trim(),
        provider: newProvider
      })

      setNewName('')
      setNewDir('')
      await refreshAccounts()
      notify({ kind: 'info', message: `Added — ${probe}` })
    } catch (error) {
      // The backend probes before persisting, so this message is the real
      // reason (wrong version, never logged in, bad path) rather than a
      // generic failure.
      notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not add account' })
    } finally {
      setBusy(false)
    }
  }

  // Committed on blur/Enter rather than per-keystroke: each save is a full
  // config round trip, and these are free-text paths the user types into.
  const commitClaudeField = async (key: ClaudeCliKey) => {
    const value = (claudeCli[key] ?? '').trim()

    try {
      const config = await getHermesConfigRecord()

      if (String(getNested(config, key) ?? '') === value) {
        return // nothing changed — don't churn the config file on every blur
      }

      await saveHermesConfig(setNested(config, key, value))
      onConfigSaved?.()
      notify({ kind: 'info', message: 'Saved. Applies to the next Claude Code session.' })
    } catch (error) {
      notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save' })
    }
  }

  const activateAccount = async (name: string) => {
    try {
      const { model } = await activateCliAccount(name)
      await refreshAccounts()
      // Reflect the runtime flip the backend just made, so the toggle above
      // doesn't keep showing "Off" until the next page load.
      setRuntimes(current => ({ ...current, model_anthropic_runtime: 'claude_code_sdk' }))
      onConfigSaved?.()
      notify({
        kind: 'info',
        message: model ? `Using "${name}" — model set to ${model}.` : `Using "${name}".`
      })
    } catch (error) {
      notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not switch account' })
    }
  }

  const removeAccount = async (name: string) => {
    try {
      await deleteCliAccount(name)
      await refreshAccounts()
      notify({ kind: 'info', message: `Removed "${name}"` })
    } catch (error) {
      notify({ kind: 'error', message: error instanceof Error ? error.message : 'Could not remove account' })
    }
  }

  const canSubmit = Boolean(newName.trim() && newDir.trim()) && !busy

  return (
    <SettingsContent>
      <SectionHeading icon={Terminal} title="CLI runtimes" />
      <p className="mb-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
        Run a turn through an external coding CLI instead of the provider’s HTTP API. Each requires
        that CLI installed and signed in; both are off by default.
      </p>

      {RUNTIMES.map(runtime => (
        <ListRow
          action={
            <Select
              onValueChange={value => void setRuntime(runtime.configKey, value)}
              value={runtimes[runtime.configKey] ?? 'auto'}
            >
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Off (use HTTP API)</SelectItem>
                <SelectItem value={runtime.enabledValue}>Use {runtime.cli} CLI</SelectItem>
              </SelectContent>
            </Select>
          }
          description={runtime.description}
          key={runtime.configKey}
          title={runtime.label}
          wide
        />
      ))}

      <SectionHeading icon={Terminal} title="Claude Code CLI" />
      <p className="mb-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
        How this instance launches the <code>claude</code> binary. All optional — leave blank to use
        the CLI’s own defaults.
      </p>

      {CLAUDE_CLI_FIELDS.map(field => (
        <ListRow
          action={
            <Input
              className="w-72"
              onBlur={() => void commitClaudeField(field.configKey)}
              onChange={event =>
                setClaudeCli(current => ({ ...current, [field.configKey]: event.target.value }))
              }
              onKeyDown={event => {
                if (event.key === 'Enter') {
                  void commitClaudeField(field.configKey)
                }
              }}
              placeholder={field.placeholder}
              value={claudeCli[field.configKey] ?? ''}
            />
          }
          description={field.description}
          key={field.configKey}
          title={field.label}
          wide
        />
      ))}

      <SectionHeading icon={Terminal} title="CLI accounts" />
      <p className="mb-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
        Isolated home directories (CLAUDE_CONFIG_DIR / CODEX_HOME) that the external CLI signs into
        itself, letting you keep separate logins. Adding one verifies the CLI version and that the
        directory actually holds credentials. To switch mid-conversation, use{' '}
        <code>/cli-account &lt;provider&gt; &lt;name&gt;</code> in chat.
      </p>

      {accounts.length === 0 ? (
        <p className="mb-3 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          No CLI accounts registered.
        </p>
      ) : (
        accounts.map(account => (
          <ListRow
            action={
              <span className="flex items-center gap-1">
                {account.provider === 'claude_code_sdk' && (
                  <Button
                    disabled={account.active}
                    onClick={() => void activateAccount(account.name)}
                    size="sm"
                    variant={account.active ? 'ghost' : 'outline'}
                  >
                    {account.active ? 'In use' : 'Use'}
                  </Button>
                )}
                <Button onClick={() => void removeAccount(account.name)} size="sm" variant="ghost">
                  <Trash2 className="size-4" />
                </Button>
              </span>
            }
            description={account.config_dir}
            key={`${account.provider}:${account.name}`}
            title={`${account.name} — ${providerLabel(account.provider)}`}
            wide
          />
        ))
      )}

      <div className="mt-3 grid gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="w-40"
            onChange={event => setNewName(event.target.value)}
            placeholder="Account name"
            value={newName}
          />
          <Select
            onValueChange={value => setNewProvider(value as CliAccount['provider'])}
            value={newProvider}
          >
            <SelectTrigger className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="claude_code_sdk">Claude Code</SelectItem>
              <SelectItem value="codex">Codex</SelectItem>
            </SelectContent>
          </Select>
          <Input
            className="min-w-56 flex-1"
            onChange={event => setNewDir(event.target.value)}
            placeholder={newProvider === 'claude_code_sdk' ? '~/.claude-personal' : '~/.codex-work'}
            value={newDir}
          />
          <Button disabled={!canSubmit} onClick={() => void submitAccount()} size="sm">
            {busy ? 'Verifying…' : 'Add account'}
          </Button>
        </div>
      </div>
    </SettingsContent>
  )
}
