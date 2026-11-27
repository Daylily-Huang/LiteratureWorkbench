import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StudyRelationDraft } from "./StudyRelationDraft";
import { StudyHistory } from "./StudyHistory";
import type { CachedStudyRecord } from "./studyExport";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const quote = (id: string) => ({ document_id: id, display_name: id+'.pdf', physical_page: 2,
  excerpt: id+' original source evidence.', rects: [], verified: true, content_id: id.repeat(64) });
const entry: CachedStudyRecord = { cache_key: 'a'.repeat(64), question: 'Compare original group', created_at: '2026-10-23', model: 'test', context_kind: 'full_text', current: true, invalid: false,
  documents: ['a','b','c'].map(id => ({ document_id: id, display_name: id+'.pdf', text_status: 'current', read_mode: 'full_text' })),
  claims: [{ conclusion: 'A bounded comparison.', evidence: [quote('a'),quote('b'),quote('c')] }], limitations: ['Uncertainty remains'] };
const receipt = { id: 'analysis', document_ids: ['a','b','c'], api_call_required: false, cache_hit: false };

it('sends only the saved claim and explicitly selected evidence indices, then opens review', async () => {
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/current') ? { csrf_token: 'csrf' } : receipt)));
  vi.stubGlobal('fetch', fetcher); const open = vi.fn();
  render(<StudyRelationDraft entry={entry} claimIndex={0} onReview={open} />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('将此结论送入关系审核 · 不调用 API'));
  fireEvent.change(screen.getByLabelText('结论1左侧依据'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿并打开关系审核' }));
  await waitFor(() => expect(open).toHaveBeenCalledWith(receipt));
  const post = fetcher.mock.calls.find(c => c[0].endsWith('/from-study'))!;
  expect(JSON.parse((post as unknown as [string, RequestInit])[1].body as string)).toEqual({ cache_key: entry.cache_key, claim_index: 0, source_index: 2, target_index: 1 });
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it('disables stale groups and same-document pairs, even when two selected quotations look valid', () => {
  const view = render(<StudyRelationDraft entry={{ ...entry, current: false }} claimIndex={0} onReview={vi.fn()} />);
  fireEvent.click(screen.getByText('将此结论送入关系审核 · 不调用 API'));
  expect(screen.getByRole('button')).toBeDisabled();
  view.rerender(<StudyRelationDraft entry={entry} claimIndex={0} onReview={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('结论1右侧依据'), { target: { value: '0' } });
  expect(screen.getByRole('button')).toBeDisabled();
});

it('rejects stale evidence discovered on the server and does not send a model request', async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith('/current') ? new Response(JSON.stringify({ csrf_token: 'csrf' })) : new Response(JSON.stringify({ code: 'study_evidence_changed' }), { status: 409 }));
  vi.stubGlobal('fetch', fetcher); const open = vi.fn();
  render(<StudyRelationDraft entry={entry} claimIndex={0} onReview={open} />);
  fireEvent.click(screen.getByText('将此结论送入关系审核 · 不调用 API'));
  fireEvent.click(screen.getByRole('button'));
  expect(await screen.findByRole('alert')).toHaveTextContent('原组正文或依据已变化');
  expect(open).not.toHaveBeenCalled(); expect(fetcher).toHaveBeenCalledTimes(2);
});

it('does not navigate to an old claim after the dialog unmounts while the response is late', async () => {
  let finish!: (r: Response) => void;
  const fetcher = vi.fn(async (url: string) => url.endsWith('/current') ? new Response(JSON.stringify({ csrf_token: 'csrf' })) : new Promise<Response>(resolve => { finish = resolve; }));
  vi.stubGlobal('fetch', fetcher); const open = vi.fn();
  const view = render(<StudyRelationDraft entry={entry} claimIndex={0} onReview={open} />);
  fireEvent.click(screen.getByText('将此结论送入关系审核 · 不调用 API')); fireEvent.click(screen.getByRole('button'));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2)); view.unmount();
  await act(async () => finish(new Response(JSON.stringify(receipt))));
  expect(open).not.toHaveBeenCalled();
});

it('makes the bridge available in the cached study dialog but keeps single-paper conclusions separate', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ total: 1, items: [entry], has_more: false, display_name: 'a.pdf' }))));
  render(<StudyHistory documentId="a" onClose={vi.fn()} onJump={vi.fn()} onReread={vi.fn()} onReview={vi.fn()} />);
  expect(await screen.findByText('将此结论送入关系审核 · 不调用 API')).toBeVisible();
});
