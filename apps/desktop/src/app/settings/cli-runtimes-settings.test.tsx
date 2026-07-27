import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const addCliAccount = vi.fn()
const deleteCliAccount = vi.fn()
const getHermesConfigRecord = vi.fn()
const listCliAccounts = vi.fn()
const saveHermesConfig = vi.fn()
const activateCliAccount = vi.fn()
const notify = vi.fn()

vi.mock('@/hermes', () => ({
  activateCliAccount: (...a: unknown[]) => activateCliAccount(...a),
  addCliAccount: (...a: unknown[]) => addCliAccount(...a),
  deleteCliAccount: (...a: unknown[]) => deleteCliAccount(...a),
  getHermesConfigRecord: () => getHermesConfigRecord(),
  listCliAccounts: () => listCliAccounts(),
  saveHermesConfig: (...a: unknown[]) => saveHermesConfig(...a)
}))

vi.mock('@/store/notifications', () => ({ notify: (...a: unknown[]) => notify(...a) }))

const { CliRuntimesSettings } = await import('./cli-runtimes-settings')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function seed(config: Record<string, unknown> = {}, accounts: unknown[] = []) {
  getHermesConfigRecord.mockResolvedValue({ model: 'claude-sonnet-5', ...config })
  listCliAccounts.mockResolvedValue({ accounts })
  saveHermesConfig.mockResolvedValue({ ok: true })
}

describe('CliRuntimesSettings', () => {
  it('renders both runtime toggles', async () => {
    seed()
    render(<CliRuntimesSettings />)

    // "Claude Code" / "Codex" each appear twice — once as the runtime row
    // title, once as an option in the add-account provider select — so assert
    // on the unambiguous per-runtime descriptions instead.
    expect(await screen.findByText(/hand each anthropic turn/i)).toBeTruthy()
    expect(screen.getByText(/hand each openai\/codex turn/i)).toBeTruthy()
  })

  it('reflects an enabled runtime from config', async () => {
    seed({ model_anthropic_runtime: 'claude_code_sdk' })
    render(<CliRuntimesSettings />)

    expect(await screen.findByText('Use claude CLI')).toBeTruthy()
  })

  it('renders the Claude CLI fields with values from config', async () => {
    seed({ claude_code: { binary_path: '/opt/claude/bin/claude', extra_args: '--chrome' } })
    render(<CliRuntimesSettings />)

    expect(await screen.findByDisplayValue('/opt/claude/bin/claude')).toBeTruthy()
    expect(screen.getByDisplayValue('--chrome')).toBeTruthy()
    expect(screen.getByText('Path to the Claude binary used by this instance.')).toBeTruthy()
    expect(screen.getByText('Additional CLI arguments passed on session start.')).toBeTruthy()
  })

  it('saves a Claude CLI field on blur, nesting it under claude_code', async () => {
    seed()
    render(<CliRuntimesSettings />)

    const input = await screen.findByPlaceholderText('claude')
    fireEvent.change(input, { target: { value: '/opt/claude/bin/claude' } })
    fireEvent.blur(input)

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalledTimes(1))
    const saved = saveHermesConfig.mock.calls[0][0] as Record<string, Record<string, string>>

    expect(saved.claude_code.binary_path).toBe('/opt/claude/bin/claude')
  })

  it('does not re-save when the value is unchanged', async () => {
    seed({ claude_code: { binary_path: '/opt/claude/bin/claude' } })
    render(<CliRuntimesSettings />)

    const input = await screen.findByDisplayValue('/opt/claude/bin/claude')
    fireEvent.blur(input)

    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())
    expect(saveHermesConfig).not.toHaveBeenCalled()
  })

  it('lists registered accounts with their config dir', async () => {
    seed({}, [{ config_dir: '/home/u/.claude-personal', name: 'personal', provider: 'claude_code_sdk' }])
    render(<CliRuntimesSettings />)

    expect(await screen.findByText('personal — Claude Code')).toBeTruthy()
    expect(screen.getByText('/home/u/.claude-personal')).toBeTruthy()
  })

  it('marks the active account as in use and offers Use on the others', async () => {
    seed({}, [
      { active: true, config_dir: '/a', name: 'personal', provider: 'claude_code_sdk' },
      { active: false, config_dir: '/b', name: 'work', provider: 'claude_code_sdk' }
    ])
    render(<CliRuntimesSettings />)

    const inUse = await screen.findByRole('button', { name: 'In use' })
    expect((inUse as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Use' })).toBeTruthy()
  })

  it('activating an account reports the model it switched to', async () => {
    seed({}, [{ active: false, config_dir: '/b', name: 'work', provider: 'claude_code_sdk' }])
    activateCliAccount.mockResolvedValue({ account: 'work', model: 'claude-fable-5', ok: true })
    render(<CliRuntimesSettings />)

    fireEvent.click(await screen.findByRole('button', { name: 'Use' }))

    await waitFor(() => expect(activateCliAccount).toHaveBeenCalledWith('work'))
    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith({
        kind: 'info',
        message: 'Using "work" — model set to claude-fable-5.'
      })
    )
  })

  it('shows an empty state when nothing is registered', async () => {
    seed()
    render(<CliRuntimesSettings />)

    expect(await screen.findByText('No CLI accounts registered.')).toBeTruthy()
  })

  it('keeps Add disabled until both name and directory are filled', async () => {
    seed()
    render(<CliRuntimesSettings />)

    const button = await screen.findByRole('button', { name: /add account/i })
    expect((button as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText('Account name'), { target: { value: 'personal' } })
    expect((button as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText('~/.claude-personal'), {
      target: { value: '/home/u/.claude-personal' }
    })
    expect((button as HTMLButtonElement).disabled).toBe(false)
  })

  it('surfaces the probe failure reason rather than a generic error', async () => {
    seed()
    addCliAccount.mockRejectedValue(new Error('no .credentials.json found under /nope'))
    render(<CliRuntimesSettings />)

    fireEvent.change(await screen.findByPlaceholderText('Account name'), { target: { value: 'x' } })
    fireEvent.change(screen.getByPlaceholderText('~/.claude-personal'), { target: { value: '/nope' } })
    fireEvent.click(screen.getByRole('button', { name: /add account/i }))

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith({
        kind: 'error',
        message: 'no .credentials.json found under /nope'
      })
    )
  })

  it('refreshes the list after a successful add', async () => {
    seed()
    addCliAccount.mockResolvedValue({ ok: true, probe: 'claude 2.1.220' })
    render(<CliRuntimesSettings />)

    fireEvent.change(await screen.findByPlaceholderText('Account name'), { target: { value: 'personal' } })
    fireEvent.change(screen.getByPlaceholderText('~/.claude-personal'), { target: { value: '/b' } })
    fireEvent.click(screen.getByRole('button', { name: /add account/i }))

    await waitFor(() => expect(addCliAccount).toHaveBeenCalledTimes(1))
    // Once on mount, once after the add.
    await waitFor(() => expect(listCliAccounts).toHaveBeenCalledTimes(2))
  })
})
