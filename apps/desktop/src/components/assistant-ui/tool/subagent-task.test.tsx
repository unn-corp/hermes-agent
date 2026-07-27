import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'
import { $toolDisclosureStates } from '@/store/tool-view'

import type { ToolPart } from './fallback-model'
import { SubagentTask } from './subagent-task'

function args(overrides: Record<string, unknown> = {}) {
  return { task_id: 'task-1', description: 'Investigate flaky test', ...overrides }
}

function part(overrides: Partial<ToolPart> = {}): ToolPart {
  return {
    args: args(),
    toolCallId: 'task-1',
    toolName: 'claude_subagent_task',
    type: 'tool-call',
    ...overrides
  } as unknown as ToolPart
}

function completedPart() {
  return part({
    result: JSON.stringify({
      task_id: 'task-1',
      status: 'completed',
      summary: 'Found the root cause',
      is_error: false
    })
  } as Partial<ToolPart>)
}

function mockGateway(response: unknown) {
  const request = vi.fn().mockResolvedValue(response)
  $gateway.set({ request } as unknown as HermesGateway)

  return request
}

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  $gateway.set(null)
  // Disclosure state is a GLOBAL store keyed by toolCallId, so without this
  // reset a test that expands `tool:task-1` leaves the next test's identical
  // part already open — and its first click would collapse rather than expand.
  $toolDisclosureStates.set({})
})

describe('SubagentTask', () => {
  it('renders the collapsed header with the task description', () => {
    render(<SubagentTask {...(part() as any)} />)

    expect(screen.getByText('Investigate flaky test')).toBeTruthy()
  })

  it('shows a running state before a result arrives', () => {
    render(<SubagentTask {...(part({ result: undefined }) as any)} />)

    expect(screen.getByText(/running/i)).toBeTruthy()
  })

  it('shows a completed state once the compact result lands', () => {
    render(<SubagentTask {...(completedPart() as any)} />)

    expect(screen.getByText('Found the root cause')).toBeTruthy()
  })

  it('fetches the full transcript via subagent_transcript.get on expand', async () => {
    $activeSessionId.set('sess-1')

    const request = mockGateway({
      found: true,
      task_id: 'task-1',
      description: 'Investigate flaky test',
      status: 'completed',
      events: [
        { type: 'text', text: 'looking at logs' },
        { type: 'tool_use', name: 'Bash', input: { command: 'pytest -k flaky' } }
      ],
      summary: 'Found the root cause'
    })

    render(<SubagentTask {...(completedPart() as any)} />)

    fireEvent.click(screen.getByRole('button', { name: /Investigate flaky test/i }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('subagent_transcript.get', {
        session_id: 'sess-1',
        task_id: 'task-1'
      })
    })
    expect(await screen.findByText('looking at logs')).toBeTruthy()
    expect(screen.getByText(/pytest -k flaky/)).toBeTruthy()
  })

  it('does not re-fetch the transcript on a second expand', async () => {
    $activeSessionId.set('sess-1')

    const request = mockGateway({
      found: true,
      task_id: 'task-1',
      events: [{ type: 'text', text: 'looking at logs' }],
      summary: 'Found the root cause'
    })

    render(<SubagentTask {...(completedPart() as any)} />)

    const toggle = screen.getByRole('button', { name: /Investigate flaky test/i })
    fireEvent.click(toggle)
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))

    fireEvent.click(toggle) // collapse
    fireEvent.click(toggle) // expand again

    await waitFor(() => expect(screen.getByText('looking at logs')).toBeTruthy())
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('does not call the gateway when there is no active session', () => {
    const request = mockGateway({ found: false })

    render(<SubagentTask {...(completedPart() as any)} />)

    fireEvent.click(screen.getByRole('button', { name: /Investigate flaky test/i }))

    expect(request).not.toHaveBeenCalled()
  })
})
