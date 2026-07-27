'use client'

import { type ToolCallMessagePartProps } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { type FC, useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { $gateway } from '@/store/gateway'
import { $toolDisclosureOpen, setToolDisclosureOpen } from '@/store/tool-view'

import { parseMaybeObject } from './fallback-model/format'
import { toolPartDisclosureId } from './fallback-model/targets'
import type { ToolPart } from './fallback-model/types'

// Copy is deliberately plain English literals, not routed through useI18n() —
// localizing this would mean editing all four locale files plus types.ts with
// translations this change cannot authentically produce. Explicit scope
// decision, not an oversight; a follow-up can localize it.
const COPY = {
  loading: 'Loading transcript…',
  running: 'Running…'
}

interface SubagentTaskArgs {
  task_id?: string
  description?: string
}

interface SubagentTaskResult {
  task_id?: string
  status?: string
  summary?: string
  is_error?: boolean
}

interface SubagentTranscriptEvent {
  type: 'text' | 'tool_use' | 'tool_result'
  text?: string
  name?: string
  input?: Record<string, unknown>
  content?: string
  is_error?: boolean
}

interface SubagentTranscriptResponse {
  found: boolean
  events?: SubagentTranscriptEvent[]
  summary?: string
}

function readArgs(args: unknown): SubagentTaskArgs {
  const row = parseMaybeObject(args)

  return {
    task_id: typeof row.task_id === 'string' ? row.task_id : undefined,
    description: typeof row.description === 'string' ? row.description : undefined
  }
}

function readResult(result: unknown): SubagentTaskResult {
  if (result === undefined) {
    return {}
  }

  const row = parseMaybeObject(result)

  return {
    task_id: typeof row.task_id === 'string' ? row.task_id : undefined,
    status: typeof row.status === 'string' ? row.status : undefined,
    summary: typeof row.summary === 'string' ? row.summary : undefined,
    is_error: row.is_error === true
  }
}

function TranscriptEventRow({ event }: { event: SubagentTranscriptEvent }) {
  if (event.type === 'text') {
    return <p className="whitespace-pre-wrap text-(--ui-text-secondary)">{event.text}</p>
  }

  if (event.type === 'tool_use') {
    return (
      <p className="font-mono text-xs text-(--ui-text-tertiary)">
        {event.name}({JSON.stringify(event.input ?? {})})
      </p>
    )
  }

  return (
    <p
      className={
        event.is_error
          ? 'whitespace-pre-wrap text-destructive'
          : 'whitespace-pre-wrap text-(--ui-text-tertiary)'
      }
    >
      {event.content}
    </p>
  )
}

export const SubagentTask: FC<ToolCallMessagePartProps> = props => {
  const { args, result } = props
  const fromArgs = useMemo(() => readArgs(args), [args])
  const fromResult = useMemo(() => readResult(result), [result])
  const gateway = useStore($gateway)
  const sessionId = useStore(useSessionView().$runtimeId)

  const disclosureId = useMemo(() => toolPartDisclosureId(props as unknown as ToolPart), [props])
  const open = useStore(useMemo(() => $toolDisclosureOpen(disclosureId), [disclosureId])) ?? false

  const [transcript, setTranscript] = useState<SubagentTranscriptResponse | null>(null)
  const [loading, setLoading] = useState(false)

  const taskId = fromResult.task_id ?? fromArgs.task_id ?? ''
  const description = fromArgs.description ?? ''
  const status = fromResult.status ?? 'running'
  const summary = fromResult.summary

  const toggle = async () => {
    const next = !open
    setToolDisclosureOpen(disclosureId, next)

    // Fetch once, on first expand only — the transcript is durable, so a
    // collapse/re-expand cycle must not re-hit the gateway.
    if (next && !transcript && gateway && sessionId && taskId) {
      setLoading(true)
      try {
        const response = await gateway.request<SubagentTranscriptResponse>(
          'subagent_transcript.get',
          { session_id: sessionId, task_id: taskId }
        )
        setTranscript(response)
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="my-1.5 rounded-md border border-primary/20 bg-(--ui-chat-surface-background) px-2.5 py-2 text-sm">
      <button aria-expanded={open} onClick={() => void toggle()} type="button">
        {description}
      </button>
      <p
        className={
          fromResult.is_error
            ? 'text-xs text-destructive'
            : 'text-xs text-(--ui-text-tertiary)'
        }
      >
        {status === 'running' ? COPY.running : summary}
      </p>
      {open && (
        <div className="mt-2 grid gap-1 border-t border-(--ui-stroke-tertiary) pt-2">
          {loading && <p className="text-xs text-(--ui-text-tertiary)">{COPY.loading}</p>}
          {transcript?.events?.map((event, index) => (
            <TranscriptEventRow event={event} key={index} />
          ))}
        </div>
      )}
    </div>
  )
}
