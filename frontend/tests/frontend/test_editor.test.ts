/**
 * Spec-first tests for Editor tab (frontend/src/tabs/editor.ts).
 * Tests cover: pipeline operations (split/merge/move/delete), span display,
 * confidence review UI, TTS rendering.
 *
 * NOTE: Run `npm test` from frontend/ to execute this suite with vitest
 * (^4.1.10) + jsdom (^30.0.1) — both are devDependencies in
 * frontend/package.json; the "test" script is `vitest` with jsdom.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  PipelineSpan,
  ReviewItem,
  pipelineOperation,
  pipelineReviewItems,
  pipelineReviewAccept,
  pipelineReviewReject,
  pipelineReviewOverride,
  pipelineRenderAudiobook,
  pipelineExportSpans,
  toPipelineSpans,
  loadSpans,
  renderSpanRow,
  handleSplit,
  handleMerge,
  handleMove,
  handleDelete,
  toggleSpanSelection,
  loadReviewItems,
  renderReviewItem,
  handleReviewAccept,
  handleReviewReject,
  handleReviewOverride,
  pipelineRenderAll,
  cancelPipelineRender,
  getCachedSpans,
  getCachedReviewItems,
  getSelectedIndices,
  handlePreviewSpan,
  setPreviewPlayer,
  playPipelineAudiobook,
  playSpanSequence,
  initEditor,
} from '../../src/tabs/editor';
import { state } from '../../src/state';
import { getPreviewPlayer } from '../../src/player';
import * as API from '../../src/api';

// Mock the API module
vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  handleError: vi.fn(),
}));

// Mock utils to avoid DOM side effects
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  // Match the real utils.escapeHtml contract: null/undefined -> ''
  escapeHtml: (s: unknown) => (s == null ? '' : String(s).replace(/</g, '&lt;').replace(/>/g, '&gt;')),
}));

// Mock templates module
vi.mock('../../src/templates', () => ({
  buildSpeakerSelect: vi.fn(() => '<select></select>'),
  updateChunkRow: vi.fn(),
}));

// Mock script module
vi.mock('../../src/tabs/script', () => ({
  pollLogs: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Test data fixtures
// ---------------------------------------------------------------------------

const MOCK_SPANS_RAW = [
  { speaker: 'Narrator', text: 'It was a dark and stormy night.', instruct: 'dramatic' },
  { speaker: 'Elizabeth', text: 'I cannot believe it!', instruct: 'surprised' },
  { speaker: 'Darcy', text: 'Forgive me, I was wrong.', instruct: 'sincere' },
];

const MOCK_SPANS: PipelineSpan[] = [
  { global_index: 1, speaker: 'Narrator', text: 'It was a dark and stormy night.', instruct: 'dramatic' },
  { global_index: 2, speaker: 'Elizabeth', text: 'I cannot believe it!', instruct: 'surprised' },
  { global_index: 3, speaker: 'Darcy', text: 'Forgive me, I was wrong.', instruct: 'sincere' },
];

const MOCK_REVIEW_ITEMS: ReviewItem[] = [
  {
    item_id: 'review-001',
    character_id: 'char-001',
    character_name: 'Elizabeth Bennet',
    confidence: 0.65,
    junction_table: 'speaker',
    related_entity_id: 'span-001',
    reason: 'Alias match: "Lizzy"',
    walk_name: 'attribution',
  },
  {
    item_id: 'review-002',
    character_id: 'char-003',
    character_name: 'Unknown Speaker',
    confidence: 0.52,
    junction_table: 'narrator',
    related_entity_id: 'span-002',
    reason: 'Low confidence match',
    walk_name: 'attribution',
  },
];

// ---------------------------------------------------------------------------
// index.html fixture resolution
// ---------------------------------------------------------------------------
// Read frontend/index.html robustly regardless of the vitest cwd: prefer a
// path derived from this file's own location (import.meta.url → fileURLToPath)
// and fall back to process.cwd() when the runner does not expose a file:// URL
// (jsdom transforms import.meta.url to an http URL in this setup).

function readIndexHtml(): string {
  try {
    const viaMeta = fileURLToPath(new URL('../../index.html', import.meta.url));
    if (existsSync(viaMeta)) return readFileSync(viaMeta, 'utf8');
  } catch {
    // import.meta.url is not a file:// URL under this runner — fall back.
  }
  return readFileSync(resolve(process.cwd(), 'index.html'), 'utf8');
}

// ---------------------------------------------------------------------------
// Test suites
// ---------------------------------------------------------------------------

describe('Editor Tab — Pipeline API Functions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
  });

  afterEach(() => {
    state.pipelineBookId = null;
  });

  describe('pipelineOperation', () => {
    it('should POST to /api/pipeline/operation with operation type and params', async () => {
      const mockResponse = { status: 'ok', operation: 'split' };
      vi.mocked(API.post).mockResolvedValue(mockResponse);

      const result = await pipelineOperation('split', {
        presentation_index: 5,
        split_point: 100,
      });

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'split',
        book_id: 'book-123',
        presentation_index: 5,
        split_point: 100,
      });
      expect(result).toEqual(mockResponse);
    });

    it('should support merge operation with left/right indices', async () => {
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'merge' });

      await pipelineOperation('merge', {
        presentation_index_left: 3,
        presentation_index_right: 4,
      });

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'merge',
        book_id: 'book-123',
        presentation_index_left: 3,
        presentation_index_right: 4,
      });
    });

    it('should support move operation with from/to indices', async () => {
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'move' });

      await pipelineOperation('move', {
        presentation_index_from: 2,
        presentation_index_to: 5,
      });

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'move',
        book_id: 'book-123',
        presentation_index_from: 2,
        presentation_index_to: 5,
      });
    });

    it('should support delete operation with presentation index', async () => {
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'delete' });

      await pipelineOperation('delete', { presentation_index: 7 });

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'delete',
        book_id: 'book-123',
        presentation_index: 7,
      });
    });
  });

  describe('pipelineReviewItems', () => {
    it('should GET /api/pipeline/review/{book_id}', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_REVIEW_ITEMS);

      const items = await pipelineReviewItems();

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/review/book-123');
      expect(items).toEqual(MOCK_REVIEW_ITEMS);
    });

    it('should return empty array if no book onboarded', async () => {
      state.pipelineBookId = null;

      const items = await pipelineReviewItems();

      expect(items).toEqual([]);
      expect(API.get).not.toHaveBeenCalled();
    });
  });

  describe('pipelineReviewAccept', () => {
    it('should POST to /api/pipeline/review/accept with item_id', async () => {
      const mockResponse = { status: 'ok', item_id: 'review-001' };
      vi.mocked(API.post).mockResolvedValue(mockResponse);

      const result = await pipelineReviewAccept('review-001');

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/review/accept', {
        item_id: 'review-001',
      });
      expect(result).toEqual(mockResponse);
    });
  });

  describe('pipelineReviewReject', () => {
    it('should POST to /api/pipeline/review/reject with item_id', async () => {
      const mockResponse = { status: 'ok', item_id: 'review-002' };
      vi.mocked(API.post).mockResolvedValue(mockResponse);

      const result = await pipelineReviewReject('review-002');

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/review/reject', {
        item_id: 'review-002',
      });
      expect(result).toEqual(mockResponse);
    });
  });

  describe('pipelineReviewOverride', () => {
    it('should POST to /api/pipeline/review/override with item_id and new_value', async () => {
      const mockResponse = { status: 'ok', item_id: 'review-001' };
      vi.mocked(API.post).mockResolvedValue(mockResponse);

      const newValue = { relation_type: 'speaker', character_id: 'char-005' };
      const result = await pipelineReviewOverride('review-001', newValue);

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/review/override', {
        item_id: 'review-001',
        new_value: newValue,
      });
      expect(result).toEqual(mockResponse);
    });
  });

  describe('pipelineRenderAudiobook', () => {
    it('should POST to /api/pipeline/render with book_id and batch options', async () => {
      const mockResponse = { job_id: 'job-abc-123' };
      vi.mocked(API.post).mockResolvedValue(mockResponse);

      const result = await pipelineRenderAudiobook(true, 42);

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/render', {
        book_id: 'book-123',
        use_batch: true,
        batch_seed: 42,
      });
      expect(result).toEqual(mockResponse);
    });

    it('should default use_batch to true and batch_seed to null', async () => {
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-xyz' });

      await pipelineRenderAudiobook();

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/render', {
        book_id: 'book-123',
        use_batch: true,
        batch_seed: null,
      });
    });
  });

  describe('pipelineExportSpans', () => {
    it('should GET /api/pipeline/export/{book_id}', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);

      const spans = await pipelineExportSpans();

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/export/book-123');
      expect(spans).toEqual(MOCK_SPANS_RAW);
    });

    it('should return empty array if no book onboarded', async () => {
      state.pipelineBookId = null;

      const spans = await pipelineExportSpans();

      expect(spans).toEqual([]);
      expect(API.get).not.toHaveBeenCalled();
    });
  });
});

describe('Editor Tab — Span Display', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
  });

  describe('toPipelineSpans', () => {
    it('should convert raw export data to PipelineSpan array with global_index', () => {
      const result = toPipelineSpans(MOCK_SPANS_RAW);

      expect(result).toHaveLength(3);
      expect(result[0]).toEqual({
        id: '',
        global_index: 1,
        speaker: 'Narrator',
        text: 'It was a dark and stormy night.',
        instruct: 'dramatic',
      });
      expect(result[1].global_index).toBe(2);
      expect(result[2].global_index).toBe(3);
    });

    it('should handle null instruct values', () => {
      const raw = [{ speaker: 'Test', text: 'Hello', instruct: null }];
      const result = toPipelineSpans(raw);

      expect(result[0].instruct).toBe('');
    });

    it('should handle missing fields', () => {
      const raw = [{ speaker: '', text: '', instruct: null }];
      const result = toPipelineSpans(raw as any);

      expect(result[0].speaker).toBe('');
      expect(result[0].text).toBe('');
      expect(result[0].instruct).toBe('');
    });
  });

  describe('renderSpanRow', () => {
    it('should render span with global_index badge, speaker, text, instruct', () => {
      const span: PipelineSpan = {
        global_index: 5,
        speaker: 'Elizabeth',
        text: 'I cannot believe it!',
        instruct: 'surprised',
      };

      const html = renderSpanRow(span);

      expect(html).toContain('data-index="5"');
      expect(html).toContain('#5');
      expect(html).toContain('Elizabeth');
      expect(html).toContain('I cannot believe it!');
      expect(html).toContain('surprised');
    });

    it('should render operation buttons (select, split, move, delete)', () => {
      const span: PipelineSpan = {
        global_index: 3,
        speaker: 'Darcy',
        text: 'Forgive me',
        instruct: 'sincere',
      };

      const html = renderSpanRow(span);

      expect(html).toContain('btn-select-span');
      expect(html).toContain('btn-span-split');
      expect(html).toContain('btn-span-move');
      expect(html).toContain('btn-span-delete');
      expect(html).toContain('data-index="3"');
    });

    it('should add table-active class when span is selected', () => {
      // First select the span
      toggleSpanSelection(2);

      const span: PipelineSpan = {
        global_index: 2,
        speaker: 'Darcy',
        text: 'Test',
        instruct: '',
      };

      const html = renderSpanRow(span);

      expect(html).toContain('table-active');

      // Clean up
      toggleSpanSelection(2);
    });
  });

  describe('loadSpans', () => {
    beforeEach(() => {
      document.body.innerHTML = `
        <div id="spans-table-body"></div>
        <div id="full-progress-bar"></div>
      `;
    });

    it('should fetch spans and render to #spans-table-body', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);

      await loadSpans();

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/export/book-123');
      const tbody = document.getElementById('spans-table-body');
      expect(tbody?.innerHTML).toContain('Narrator');
      expect(tbody?.innerHTML).toContain('Elizabeth');
      expect(tbody?.innerHTML).toContain('Darcy');
    });

    it('should show message when no book onboarded', async () => {
      state.pipelineBookId = null;

      await loadSpans();

      const tbody = document.getElementById('spans-table-body');
      expect(tbody?.innerHTML).toContain('No book onboarded');
    });

    it('should show message when no spans found', async () => {
      vi.mocked(API.get).mockResolvedValue([]);

      await loadSpans();

      const tbody = document.getElementById('spans-table-body');
      expect(tbody?.innerHTML).toContain('No spans found');
    });

    it('should update cached spans', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);

      await loadSpans();

      const cached = getCachedSpans();
      expect(cached).toHaveLength(3);
      expect(cached[0].speaker).toBe('Narrator');
    });
  });
});

describe('Editor Tab — Pipeline Operations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `
      <div id="spans-table-body"></div>
      <button id="btn-pipeline-merge" disabled></button>
    `;
    // Pre-load spans
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
  });

  describe('handleSplit', () => {
    it('should prompt for split point and call pipelineOperation', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      global.prompt = vi.fn().mockReturnValue('10');
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'split' });

      await handleSplit(1);

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'split',
        book_id: 'book-123',
        presentation_index: 1,
        split_point: 10,
      });
    });

    it('should show warning if no book onboarded', async () => {
      state.pipelineBookId = null;
      const { showToast } = await import('../../src/utils');

      await handleSplit(1);

      expect(showToast).toHaveBeenCalledWith('No book onboarded', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should show error for invalid split point', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      global.prompt = vi.fn().mockReturnValue('999');
      const { showToast } = await import('../../src/utils');

      await handleSplit(1);

      expect(showToast).toHaveBeenCalledWith('Invalid split point', 'error');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should not proceed if user cancels prompt', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      global.prompt = vi.fn().mockReturnValue(null);

      await handleSplit(1);

      expect(API.post).not.toHaveBeenCalled();
    });
  });

  describe('handleMerge', () => {
    it('should merge two adjacent selected spans', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(1);
      toggleSpanSelection(2);
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'merge' });

      await handleMerge();

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'merge',
        book_id: 'book-123',
        presentation_index_left: 1,
        presentation_index_right: 2,
      });
    });

    it('should show warning if not exactly 2 spans selected', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(1);
      const { showToast } = await import('../../src/utils');

      await handleMerge();

      expect(showToast).toHaveBeenCalledWith('Select exactly 2 adjacent spans to merge', 'warning');
      expect(API.post).not.toHaveBeenCalled();
      toggleSpanSelection(1); // cleanup: clear leaked selection
    });

    it('should show warning if spans are not adjacent', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(1);
      toggleSpanSelection(3);
      const { showToast } = await import('../../src/utils');

      await handleMerge();

      expect(showToast).toHaveBeenCalledWith('Can only merge adjacent spans', 'warning');
      expect(API.post).not.toHaveBeenCalled();
      toggleSpanSelection(1);
      toggleSpanSelection(3); // cleanup: clear leaked selection
    });
  });

  describe('handleMove', () => {
    it('should move selected span to target position', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(1);
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'move' });

      await handleMove(3);

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'move',
        book_id: 'book-123',
        presentation_index_from: 1,
        presentation_index_to: 3,
      });
    });

    it('should show warning if not exactly 1 span selected', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      const { showToast } = await import('../../src/utils');

      await handleMove(3);

      expect(showToast).toHaveBeenCalledWith('Select exactly 1 span to move', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should show info if moving to same position', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(3);
      const { showToast } = await import('../../src/utils');

      await handleMove(3);

      expect(showToast).toHaveBeenCalledWith('Span is already at that position', 'info');
      expect(API.post).not.toHaveBeenCalled();
      toggleSpanSelection(3); // cleanup: clear leaked selection
    });
  });

  describe('handleDelete', () => {
    it('should confirm and delete span', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      const { showConfirm } = await import('../../src/utils');
      vi.mocked(showConfirm).mockResolvedValue(true);
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'delete' });

      await handleDelete(1);

      expect(showConfirm).toHaveBeenCalled();
      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'delete',
        book_id: 'book-123',
        presentation_index: 1,
      });
    });

    it('should not delete if user cancels confirmation', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      const { showConfirm } = await import('../../src/utils');
      vi.mocked(showConfirm).mockResolvedValue(false);

      await handleDelete(1);

      expect(API.post).not.toHaveBeenCalled();
    });
  });

  describe('toggleSpanSelection', () => {
    it('should add index to selection set', () => {
      toggleSpanSelection(5);

      const selected = getSelectedIndices();
      expect(selected.has(5)).toBe(true);
      toggleSpanSelection(5); // cleanup: clear leaked selection
    });

    it('should remove index if already selected', () => {
      toggleSpanSelection(3);
      toggleSpanSelection(3);

      const selected = getSelectedIndices();
      expect(selected.has(3)).toBe(false);
    });

    it('should update row visual class', () => {
      document.body.innerHTML = `
        <table><tbody id="spans-table-body">
          <tr data-index="2"><td>Test</td></tr>
        </tbody></table>
      `;

      toggleSpanSelection(2);
      const row = document.querySelector('tr[data-index="2"]');
      expect(row?.classList.contains('table-active')).toBe(true);

      toggleSpanSelection(2);
      expect(row?.classList.contains('table-active')).toBe(false);
    });
  });
});

describe('Editor Tab — Confidence Review UI', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `
      <div id="review-items-container"></div>
    `;
  });

  describe('loadReviewItems', () => {
    it('should fetch and render review items', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_REVIEW_ITEMS);

      await loadReviewItems();

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/review/book-123');
      const container = document.getElementById('review-items-container');
      expect(container?.innerHTML).toContain('Elizabeth Bennet');
      expect(container?.innerHTML).toContain('Unknown Speaker');
    });

    it('should show success message when no items need review', async () => {
      vi.mocked(API.get).mockResolvedValue([]);

      await loadReviewItems();

      const container = document.getElementById('review-items-container');
      expect(container?.innerHTML).toContain('No items need review');
    });

    it('should show message when no book onboarded', async () => {
      state.pipelineBookId = null;

      await loadReviewItems();

      const container = document.getElementById('review-items-container');
      expect(container?.innerHTML).toContain('No book onboarded');
    });

    it('should update cached review items', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_REVIEW_ITEMS);

      await loadReviewItems();

      const cached = getCachedReviewItems();
      expect(cached).toHaveLength(2);
      expect(cached[0].character_name).toBe('Elizabeth Bennet');
    });
  });

  describe('renderReviewItem', () => {
    it('should render review item with confidence badge and action buttons', () => {
      const item: ReviewItem = {
        item_id: 'review-001',
        character_id: 'char-001',
        character_name: 'Elizabeth',
        confidence: 0.65,
        junction_table: 'speaker',
        related_entity_id: 'span-001',
        reason: 'Alias match',
      };

      const html = renderReviewItem(item);

      expect(html).toContain('65% confidence');
      expect(html).toContain('Elizabeth');
      expect(html).toContain('speaker');
      expect(html).toContain('Alias match');
      expect(html).toContain('btn-review-accept');
      expect(html).toContain('btn-review-reject');
      expect(html).toContain('btn-review-override');
      expect(html).toContain('data-item-id="review-001"');
    });

    it('should use warning badge for confidence >= 60%', () => {
      const item: ReviewItem = {
        item_id: 'test',
        character_id: 'c1',
        character_name: 'Test',
        confidence: 0.65,
        junction_table: 'speaker',
        related_entity_id: 's1',
      };

      const html = renderReviewItem(item);

      expect(html).toContain('bg-warning');
    });

    it('should use danger badge for confidence < 60%', () => {
      const item: ReviewItem = {
        item_id: 'test',
        character_id: 'c1',
        character_name: 'Test',
        confidence: 0.52,
        junction_table: 'speaker',
        related_entity_id: 's1',
      };

      const html = renderReviewItem(item);

      expect(html).toContain('bg-danger');
    });
  });

  describe('handleReviewAccept', () => {
    it('should call pipelineReviewAccept and refresh review items', async () => {
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', item_id: 'review-001' });
      vi.mocked(API.get).mockResolvedValue([]);

      await handleReviewAccept('review-001');

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/review/accept', {
        item_id: 'review-001',
      });
      // Should refresh review items
      expect(API.get).toHaveBeenCalledWith('/api/pipeline/review/book-123');
    });
  });

  describe('handleReviewReject', () => {
    it('should call pipelineReviewReject and refresh review items', async () => {
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', item_id: 'review-002' });
      vi.mocked(API.get).mockResolvedValue([]);

      await handleReviewReject('review-002');

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/review/reject', {
        item_id: 'review-002',
      });
      expect(API.get).toHaveBeenCalledWith('/api/pipeline/review/book-123');
    });
  });

  describe('handleReviewOverride', () => {
    it('should prompt for JSON value and call pipelineReviewOverride', async () => {
      global.prompt = vi.fn().mockReturnValue('{"relation_type": "speaker"}');
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', item_id: 'review-001' });
      vi.mocked(API.get).mockResolvedValue([]);

      await handleReviewOverride('review-001');

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/review/override', {
        item_id: 'review-001',
        new_value: { relation_type: 'speaker' },
      });
    });

    it('should show error for invalid JSON', async () => {
      global.prompt = vi.fn().mockReturnValue('not valid json');
      const { showToast } = await import('../../src/utils');

      await handleReviewOverride('review-001');

      expect(showToast).toHaveBeenCalledWith('Invalid JSON', 'error');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should not proceed if user cancels prompt', async () => {
      global.prompt = vi.fn().mockReturnValue(null);

      await handleReviewOverride('review-001');

      expect(API.post).not.toHaveBeenCalled();
    });
  });
});

describe('Editor Tab — TTS Rendering (Pipeline Mode)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `
      <button id="btn-pipeline-render"></button>
      <button id="btn-pipeline-regen"></button>
      <button id="btn-pipeline-cancel" style="display:none;"></button>
      <span id="pipeline-render-job" class="d-none"></span>
    `;
  });

  describe('pipelineRenderAll', () => {
    afterEach(async () => {
      await cancelPipelineRender(true);
    });

    it('should call pipelineRenderAudiobook and store job id on completion', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-abc-123' });
      vi.mocked(API.get).mockResolvedValue({ job_id: 'job-abc-123', status: 'completed', output_dir: null, error: null });

      const promise = pipelineRenderAll();
      // Fire the 2s render-status poll so the job completes
      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/render', {
        book_id: 'book-123',
        use_batch: true,
        batch_seed: null,
      });
      expect(state.pipelineRenderJobId).toBe('job-abc-123');

      vi.useRealTimers();
    });

    it('should show warning if no book onboarded', async () => {
      state.pipelineBookId = null;
      const { showToast } = await import('../../src/utils');

      await pipelineRenderAll();

      expect(showToast).toHaveBeenCalledWith('No book onboarded', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should hide render buttons and show cancel button during render', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-test' });
      vi.mocked(API.get).mockResolvedValue({ job_id: 'job-test', status: 'completed', output_dir: null, error: null });

      const promise = pipelineRenderAll();

      // Buttons are swapped synchronously before the first await
      const btnRender = document.getElementById('btn-pipeline-render');
      const btnRegen = document.getElementById('btn-pipeline-regen');
      const btnCancel = document.getElementById('btn-pipeline-cancel');

      expect(btnRender?.style.display).toBe('none');
      expect(btnRegen?.style.display).toBe('none');
      expect(btnCancel?.style.display).toBe('inline-block');

      // Let the render finish (poll resolves -> finally restores buttons)
      await vi.advanceTimersByTimeAsync(2000);
      await promise;
      vi.useRealTimers();
    });

    it('should handle render failure gracefully', async () => {
      vi.mocked(API.post).mockRejectedValue(new Error('Render failed'));
      const { showToast } = await import('../../src/utils');

      await pipelineRenderAll();

      expect(showToast).toHaveBeenCalledWith('Render failed: Render failed', 'error');
    });
  });
});

describe('Editor Tab — Testability Exports', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `<div id="spans-table-body"></div>`;
  });

  it('getCachedSpans should return current cached spans', async () => {
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);

    await loadSpans();

    const cached = getCachedSpans();
    expect(cached).toHaveLength(3);
    expect(cached[0].speaker).toBe('Narrator');
  });

  it('getCachedReviewItems should return current cached review items', async () => {
    document.body.innerHTML = `<div id="review-items-container"></div>`;
    vi.mocked(API.get).mockResolvedValue(MOCK_REVIEW_ITEMS);

    await loadReviewItems();

    const cached = getCachedReviewItems();
    expect(cached).toHaveLength(2);
    expect(cached[0].character_name).toBe('Elizabeth Bennet');
  });

  it('getSelectedIndices should return a copy of selected indices', () => {
    toggleSpanSelection(1);
    toggleSpanSelection(3);

    const selected = getSelectedIndices();
    expect(selected.has(1)).toBe(true);
    expect(selected.has(3)).toBe(true);
    expect(selected.size).toBe(2);

    // Modifying returned set should not affect internal state
    selected.add(99);
    const selectedAgain = getSelectedIndices();
    expect(selectedAgain.has(99)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Per-span audio preview (audio surface — Plan E, Phase 4)
//
// Backend conventions verified from source (see plan annotations):
//   • Individual-mode render writes ONE render_chunk per span in annotated-script
//     presentation order, idx 0-BASED (tts_integration.py render_audiobook:
//     enumerate(script) → _insert_chunk_row(job_id, i, ...); script is the same
//     array GET /api/pipeline/export/{book_id} serves). So chunk idx =
//     span.global_index − 1.
//   • GET /api/pipeline/export/jobs/{job_id}/chunks → ChunkRow[] ordered by idx
//     ({job_id, idx, status, wav_path, error}); empty for batch jobs.
//   • GET /api/pipeline/render_status/{job_id} → {job_id, status, mode, ...}
//     where mode ∈ {'individual', 'batch'}.
//   • Batch jobs have no per-chunk rows → per-span preview is blocked with the
//     contract tooltip "preview differs from final — whole-book playback only".
// ---------------------------------------------------------------------------

describe('Editor Tab — Per-Span Audio Preview (Audio Surface)', () => {
  /** Injectable mock player — setPreviewPlayer swaps it for the real singleton. */
  let mockPlayer: {
    play: ReturnType<typeof vi.fn>;
    pause: ReturnType<typeof vi.fn>;
    seek: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
  };

  const MOCK_CHUNK_ROWS = [
    { job_id: 'job-preview-1', idx: 0, status: 'done', wav_path: 'chunk_0000.wav', error: null },
    { job_id: 'job-preview-1', idx: 1, status: 'done', wav_path: 'chunk_0001.wav', error: null },
    { job_id: 'job-preview-1', idx: 2, status: 'done', wav_path: 'chunk_0002.wav', error: null },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    // Clear any leftover mockResolvedValueOnce queue from prior tests in this file.
    vi.mocked(API.get).mockReset();
    state.pipelineBookId = 'book-123';
    state.pipelineRenderJobId = 'job-preview-1';
    mockPlayer = {
      play: vi.fn().mockResolvedValue(undefined),
      pause: vi.fn(),
      seek: vi.fn(),
      stop: vi.fn().mockResolvedValue(undefined),
    };
    setPreviewPlayer(mockPlayer);
  });

  afterEach(() => {
    state.pipelineRenderJobId = null;
    // Restore the real singleton player so any later tests use the default binding.
    setPreviewPlayer(getPreviewPlayer());
  });

  describe('renderSpanRow preview affordance', () => {
    it('emits a ▶ preview button with job/idx data attributes', () => {
      const span: PipelineSpan = {
        global_index: 3,
        speaker: 'Darcy',
        text: 'Forgive me, I was wrong.',
        instruct: 'sincere',
      };

      const html = renderSpanRow(span);

      expect(html).toContain('btn-span-preview');
      expect(html).toContain('data-job-id="job-preview-1"');
      // chunk idx is 0-based: global_index − 1
      expect(html).toContain('data-chunk-idx="2"');
      expect(html).toContain('fa-play');
    });

    it('emits the preview button disabled when no render job is known', () => {
      state.pipelineRenderJobId = null;
      const span: PipelineSpan = {
        global_index: 1,
        speaker: 'Narrator',
        text: 'Once upon a time',
        instruct: '',
      };

      const html = renderSpanRow(span);

      expect(html).toContain('btn-span-preview');
      expect(html).toContain('disabled');
    });
  });

  describe('handlePreviewSpan', () => {
    it('resolves the correct chunk URL and plays it via the player (individual mode)', async () => {
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-preview-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      await handlePreviewSpan(2);

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/render_status/job-preview-1');
      expect(API.get).toHaveBeenCalledWith('/api/pipeline/export/jobs/job-preview-1/chunks');
      expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/chunk/job-preview-1/1');
    });

    it('falls back to the presentation-order chunk idx when the chunk list is unavailable', async () => {
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-preview-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockRejectedValueOnce(new Error('network'));

      await handlePreviewSpan(3);

      expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/chunk/job-preview-1/2');
    });

    it('uses the render_chunk row list when available to resolve the idx', async () => {
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-preview-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      await handlePreviewSpan(2);

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/export/jobs/job-preview-1/chunks');
      expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/chunk/job-preview-1/1');
    });

    it('shows the "preview differs from final" tooltip and does not play for batch jobs', async () => {
      const { showToast } = await import('../../src/utils');
      vi.mocked(API.get).mockResolvedValue({
        job_id: 'job-preview-1',
        status: 'completed',
        mode: 'batch',
        output_dir: null,
        error: null,
      });

      await handlePreviewSpan(2);

      expect(showToast).toHaveBeenCalledWith('preview differs from final — whole-book playback only', 'warning');
      expect(mockPlayer.play).not.toHaveBeenCalled();
      // Batch jobs have no chunk rows — the chunk list must not even be fetched.
      expect(API.get).not.toHaveBeenCalledWith('/api/pipeline/export/jobs/job-preview-1/chunks');
    });

    it('shows a warning and does not play when no render job exists', async () => {
      const { showToast } = await import('../../src/utils');
      state.pipelineRenderJobId = null;

      await handlePreviewSpan(1);

      expect(showToast).toHaveBeenCalledWith('No render job to preview', 'warning');
      expect(mockPlayer.play).not.toHaveBeenCalled();
    });
  });

  describe('delegated click wiring', () => {
    it('clicking a row preview button plays the span chunk', async () => {
      document.body.innerHTML = `<table><tbody id="spans-table-body"></tbody></table>`;
      const tbody = document.getElementById('spans-table-body') as HTMLElement;
      tbody.innerHTML = renderSpanRow({
        global_index: 2,
        speaker: 'Elizabeth',
        text: 'I cannot believe it!',
        instruct: 'surprised',
      });

      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-preview-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      const btn = tbody.querySelector('.btn-span-preview') as HTMLButtonElement;
      expect(btn).not.toBeNull();
      btn.click();

      // Exactly-once: the initEditor duplicate-wiring guard means only one
      // delegated listener is ever registered for the spans table.
      await vi.waitFor(() => {
        expect(mockPlayer.play).toHaveBeenCalledTimes(1);
        expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/chunk/job-preview-1/1');
      });
    });
  });
});

// ---------------------------------------------------------------------------
// Whole-book playback (audio surface — Plan E, Phase 5)
//
// Backend contract (Plan C, CONTRACTS.md): GET /api/pipeline/export/audio/{job_id}
// serves whole-book playback — the artifact WAV when present, otherwise a
// synthesized streaming concat of the job's chunks (Range supported across
// chunk boundaries, HEAD supported). Available for BOTH individual and batch
// render modes: batch jobs have NO per-chunk rows, so whole-book playback is
// their only audio surface (see BATCH_PREVIEW_TOOLTIP).
//
// The affordance is a "Play book" button (#btn-pipeline-play-book) in the
// editor tab card header next to the #pipeline-render-job badge. It resolves
// the job id exactly like downloadPipelineRender / mergePipelineAudiobook:
//   state.pipelineRenderJobId ?? _currentRenderJobId
// and plays GET /api/pipeline/export/audio/{job_id} via the injected player
// (setPreviewPlayer — same injectable-singleton pattern as handlePreviewSpan).
// ---------------------------------------------------------------------------

describe('Editor Tab — Whole-Book Playback (Audio Surface)', () => {
  /** Injectable mock player — setPreviewPlayer swaps it for the real singleton. */
  let mockPlayer: {
    play: ReturnType<typeof vi.fn>;
    pause: ReturnType<typeof vi.fn>;
    seek: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    vi.clearAllMocks();
    // Clear any leftover mockResolvedValueOnce queue from prior tests in this file.
    vi.mocked(API.get).mockReset();
    state.pipelineBookId = 'book-123';
    state.pipelineRenderJobId = 'job-play-1';
    mockPlayer = {
      play: vi.fn().mockResolvedValue(undefined),
      pause: vi.fn(),
      seek: vi.fn(),
      stop: vi.fn().mockResolvedValue(undefined),
    };
    setPreviewPlayer(mockPlayer);
  });

  afterEach(() => {
    state.pipelineRenderJobId = null;
    // Restore the real singleton player so any later tests use the default binding.
    setPreviewPlayer(getPreviewPlayer());
  });

  describe('play book affordance in the editor tab UI', () => {
    it('index.html declares the #btn-pipeline-play-book button near the render job badge', () => {
      const html = readIndexHtml();

      expect(html).toContain('id="btn-pipeline-play-book"');
      // Spec from Plan E P5-S2: btn-outline-info, play icon, title "Play whole book".
      expect(html).toContain('btn-outline-info');
      expect(html).toContain('fa-play');
      expect(html).toContain('Play whole book');

      // The affordance must live in the editor tab card header, next to the
      // #pipeline-render-job badge (not somewhere unreachable in another tab).
      // Positional ordering is less brittle than slicing the HTML between
      // landmark ids: the button must sit inside the editor tab and the badge
      // must come after it.
      const editorTabStart = html.indexOf('id="editor-tab"');
      const playBookIdx = html.indexOf('id="btn-pipeline-play-book"');
      const renderJobIdx = html.indexOf('id="pipeline-render-job"');
      expect(editorTabStart).toBeGreaterThan(-1);
      expect(playBookIdx).toBeGreaterThan(editorTabStart);
      expect(renderJobIdx).toBeGreaterThan(playBookIdx);
    });
  });

  describe('playPipelineAudiobook', () => {
    it('plays the whole book via GET /api/pipeline/export/audio/{job_id}', async () => {
      await playPipelineAudiobook();

      expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/audio/job-play-1');
    });

    it('accepts an explicit job id (takes precedence over state)', async () => {
      await playPipelineAudiobook('job-explicit');

      expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/audio/job-explicit');
    });

    it('supports seeking on the singleton player after playback starts', async () => {
      await playPipelineAudiobook();
      expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/audio/job-play-1');

      // seek(seconds) on the real player just delegates to the audio element's
      // currentTime; with the injected spy we assert the call itself.
      mockPlayer.seek(120);
      expect(mockPlayer.seek).toHaveBeenCalledWith(120);
      // Same singleton instance serves both play and seek (whole-book surface).
      expect(mockPlayer.play.mock.instances[0]).toBe(mockPlayer.seek.mock.instances[0]);
    });

    it('shows a warning toast and does not play when no render job exists', async () => {
      const { showToast } = await import('../../src/utils');
      state.pipelineRenderJobId = null;

      await playPipelineAudiobook();

      expect(showToast).toHaveBeenCalledWith('No render job to play. Render the audiobook first.', 'warning');
      expect(mockPlayer.play).not.toHaveBeenCalled();
    });
  });

  describe('click wiring', () => {
    it('clicking the Play book button resolves the job id and plays the export/audio URL', async () => {
      document.body.innerHTML = `
        <button id="btn-pipeline-play-book"><i class="fas fa-play"></i></button>
      `;

      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      const btn = document.getElementById('btn-pipeline-play-book') as HTMLButtonElement;
      expect(btn).not.toBeNull();
      btn.click();

      // Exactly-once: the initEditor duplicate-wiring guard prevents stacked
      // click handlers from firing play multiple times.
      await vi.waitFor(() => {
        expect(mockPlayer.play).toHaveBeenCalledTimes(1);
        expect(mockPlayer.play).toHaveBeenCalledWith('/api/pipeline/export/audio/job-play-1');
      });
    });
  });
});

// ---------------------------------------------------------------------------
// Sequence playback (audio surface — Plan E, Phase 6)
//
// playSpanSequence() resolves the chunk URLs for a set of spans and plays them
// in sequence through the injected player's playSequence() queue (auto-advance
// on the element's 'ended' event). v1 semantics (DD open item #5): the
// sequence is the presentation order of the LOADED spans (_cachedSpans sorted
// by global_index); when a non-empty selection exists (_selectedIndices) the
// sequence is the selected spans sorted by global_index. Review-filtered spans
// are NOT excluded — _cachedSpans is the full export; review items never
// remove spans from the table.
//
// Chunk idx mapping is the Phase 4 convention (0-based presentation order =
// global_index − 1, verified from tts_integration.py render_audiobook
// enumerate(script) → _insert_chunk_row(job_id, i, ...)), confirmed by
// GET /api/pipeline/export/jobs/{job_id}/chunks when available.
//
// Batch jobs have no per-chunk rows, so sequence playback requires individual
// mode: a batch job shows the same contract tooltip as per-span preview
// (BATCH_PREVIEW_TOOLTIP) and nothing is queued.
// ---------------------------------------------------------------------------

describe('Editor Tab — Sequence Playback (Audio Surface)', () => {
  /** Injectable mock player — setPreviewPlayer swaps it for the real singleton. */
  let mockPlayer: {
    play: ReturnType<typeof vi.fn>;
    playSequence: ReturnType<typeof vi.fn>;
    pause: ReturnType<typeof vi.fn>;
    seek: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
  };

  const MOCK_CHUNK_ROWS = [
    { job_id: 'job-seq-1', idx: 0, status: 'done', wav_path: 'chunk_0000.wav', error: null },
    { job_id: 'job-seq-1', idx: 1, status: 'done', wav_path: 'chunk_0001.wav', error: null },
    { job_id: 'job-seq-1', idx: 2, status: 'done', wav_path: 'chunk_0002.wav', error: null },
  ];

  beforeEach(async () => {
    vi.clearAllMocks();
    // Clear any leftover mockResolvedValueOnce queue from prior tests in this file.
    vi.mocked(API.get).mockReset();
    // Clear any selection leaked by earlier describes in this file.
    for (const idx of getSelectedIndices()) toggleSpanSelection(idx);

    state.pipelineBookId = 'book-123';
    state.pipelineRenderJobId = 'job-seq-1';
    mockPlayer = {
      play: vi.fn().mockResolvedValue(undefined),
      playSequence: vi.fn().mockResolvedValue(undefined),
      pause: vi.fn(),
      seek: vi.fn(),
      stop: vi.fn().mockResolvedValue(undefined),
    };
    setPreviewPlayer(mockPlayer);

    // Seed the span cache in presentation order (loadSpans reads the export
    // endpoint into _cachedSpans).
    document.body.innerHTML = `<table><tbody id="spans-table-body"></tbody></table>`;
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
    await loadSpans();
  });

  afterEach(() => {
    state.pipelineRenderJobId = null;
    // Restore the real singleton player so any later tests use the default binding.
    setPreviewPlayer(getPreviewPlayer());
  });

  describe('playSpanSequence', () => {
    it('queues all loaded spans in presentation order (no selection)', async () => {
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-seq-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      await playSpanSequence();

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/render_status/job-seq-1');
      expect(API.get).toHaveBeenCalledWith('/api/pipeline/export/jobs/job-seq-1/chunks');
      expect(mockPlayer.playSequence).toHaveBeenCalledWith([
        '/api/pipeline/export/chunk/job-seq-1/0',
        '/api/pipeline/export/chunk/job-seq-1/1',
        '/api/pipeline/export/chunk/job-seq-1/2',
      ]);
    });

    it('queues only the selected spans, sorted by presentation order', async () => {
      toggleSpanSelection(3);
      toggleSpanSelection(1);

      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-seq-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      await playSpanSequence();

      // Selected spans {3, 1} sorted by global_index → 1 then 3 → idx 0 then 2.
      expect(mockPlayer.playSequence).toHaveBeenCalledWith([
        '/api/pipeline/export/chunk/job-seq-1/0',
        '/api/pipeline/export/chunk/job-seq-1/2',
      ]);

      // Cleanup: clear the leaked selection.
      toggleSpanSelection(3);
      toggleSpanSelection(1);
    });

    it('falls back to presentation-order chunk idx when the chunk list is unavailable', async () => {
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-seq-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockRejectedValueOnce(new Error('network'));

      await playSpanSequence();

      expect(mockPlayer.playSequence).toHaveBeenCalledWith([
        '/api/pipeline/export/chunk/job-seq-1/0',
        '/api/pipeline/export/chunk/job-seq-1/1',
        '/api/pipeline/export/chunk/job-seq-1/2',
      ]);
    });

    it('shows the "preview differs from final" tooltip and does not queue for batch jobs', async () => {
      const { showToast } = await import('../../src/utils');
      vi.mocked(API.get).mockResolvedValue({
        job_id: 'job-seq-1',
        status: 'completed',
        mode: 'batch',
        output_dir: null,
        error: null,
      });

      await playSpanSequence();

      expect(showToast).toHaveBeenCalledWith('preview differs from final — whole-book playback only', 'warning');
      expect(mockPlayer.playSequence).not.toHaveBeenCalled();
      // Batch jobs have no chunk rows — the chunk list must not even be fetched.
      expect(API.get).not.toHaveBeenCalledWith('/api/pipeline/export/jobs/job-seq-1/chunks');
    });

    it('shows a warning and does not queue when no render job exists', async () => {
      const { showToast } = await import('../../src/utils');
      state.pipelineRenderJobId = null;

      await playSpanSequence();

      expect(showToast).toHaveBeenCalledWith('No render job to play. Render the audiobook first.', 'warning');
      expect(mockPlayer.playSequence).not.toHaveBeenCalled();
    });

    it('shows a "No spans to play" toast and does not queue when the span cache is empty', async () => {
      const { showToast } = await import('../../src/utils');
      // Empty the span cache: re-run loadSpans against an empty export so the
      // ordered span set is empty (individual mode — no batch guard applies).
      vi.mocked(API.get).mockResolvedValue([]);
      await loadSpans();

      vi.mocked(API.get).mockResolvedValueOnce({
        job_id: 'job-seq-1',
        status: 'completed',
        mode: 'individual',
        output_dir: null,
        error: null,
      });

      await playSpanSequence();

      expect(showToast).toHaveBeenCalledWith('No spans to play', 'warning');
      expect(mockPlayer.playSequence).not.toHaveBeenCalled();
      // The chunk list must not be fetched for an empty span set.
      expect(API.get).not.toHaveBeenCalledWith('/api/pipeline/export/jobs/job-seq-1/chunks');
    });

    it('accepts an explicit job id (takes precedence over state)', async () => {
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-seq-explicit',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      await playSpanSequence('job-seq-explicit');

      expect(API.get).toHaveBeenCalledWith('/api/pipeline/render_status/job-seq-explicit');
      expect(API.get).toHaveBeenCalledWith('/api/pipeline/export/jobs/job-seq-explicit/chunks');
      expect(mockPlayer.playSequence).toHaveBeenCalledWith([
        '/api/pipeline/export/chunk/job-seq-explicit/0',
        '/api/pipeline/export/chunk/job-seq-explicit/1',
        '/api/pipeline/export/chunk/job-seq-explicit/2',
      ]);
    });
  });

  describe('sequence affordance in the editor tab UI', () => {
    it('index.html declares the #btn-pipeline-play-sequence button near the render job badge', () => {
      const html = readIndexHtml();

      expect(html).toContain('id="btn-pipeline-play-sequence"');

      // The affordance must live in the editor tab card header, next to the
      // #pipeline-render-job badge (like the Play Book button) — positional
      // ordering, less brittle than slicing the HTML between landmark ids.
      const editorTabStart = html.indexOf('id="editor-tab"');
      const playSeqIdx = html.indexOf('id="btn-pipeline-play-sequence"');
      const renderJobIdx = html.indexOf('id="pipeline-render-job"');
      expect(editorTabStart).toBeGreaterThan(-1);
      expect(playSeqIdx).toBeGreaterThan(editorTabStart);
      expect(renderJobIdx).toBeGreaterThan(playSeqIdx);
    });
  });

  describe('click wiring', () => {
    it('clicking the Play Sequence button queues the spans in order', async () => {
      document.body.innerHTML = `
        <button id="btn-pipeline-play-sequence"><i class="fas fa-list-ul"></i></button>
      `;

      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-seq-1',
          status: 'completed',
          mode: 'individual',
          output_dir: null,
          error: null,
        })
        .mockResolvedValueOnce(MOCK_CHUNK_ROWS);

      const btn = document.getElementById('btn-pipeline-play-sequence') as HTMLButtonElement;
      expect(btn).not.toBeNull();
      btn.click();

      // Exactly-once: the initEditor duplicate-wiring guard prevents stacked
      // click handlers from queueing the sequence multiple times.
      await vi.waitFor(() => {
        expect(mockPlayer.playSequence).toHaveBeenCalledTimes(1);
        expect(mockPlayer.playSequence).toHaveBeenCalledWith([
          '/api/pipeline/export/chunk/job-seq-1/0',
          '/api/pipeline/export/chunk/job-seq-1/1',
          '/api/pipeline/export/chunk/job-seq-1/2',
        ]);
      });
    });
  });
});
