/**
 * Spec-first tests for Editor tab (frontend/src/tabs/editor.ts).
 * Tests cover: pipeline operations (split/merge/move/delete), span display,
 * confidence review UI, TTS rendering, and the single-speaker render toggle
 * (Plan J, Phase 2): the toggle writes book.single_speaker via
 * GET/PUT /api/pipeline/book/{book_id}/single_speaker, reflects the saved
 * value on load, and reverts + surfaces an error when a write fails.
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
  pipelineGetSingleSpeaker,
  pipelineSetSingleSpeaker,
  pipelineExportM4b,
  handleExportM4bSubmit,
  initExportM4bForm,
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
  pipelineCancelRender,
  cancelPipelineRender,
  getCachedSpans,
  getCachedReviewItems,
  getSelectedIndices,
  handlePreviewSpan,
  setPreviewPlayer,
  playPipelineAudiobook,
  playSpanSequence,
  initEditor,
  loadSingleSpeakerToggle,
  handleSingleSpeakerToggleChange,
  pushUndoEntry,
  undoLastSpanEdit,
  clearUndoStack,
  getUndoStack,
  pipelineGetSpanPause,
  pipelineSetSpanPause,
} from '../../src/tabs/editor';
import { state } from '../../src/state';
import { getPreviewPlayer } from '../../src/player';
import * as API from '../../src/api';

// Mock the API module — partial mock: get/post stay mocked, but the REAL
// postWithRetryOnce is exposed so cancel tests exercise the actual
// 503+Retry-After retry-once wrapper against a spied global fetch.
vi.mock('../../src/api', async () => {
  const actual = await vi.importActual<typeof import('../../src/api')>('../../src/api');
  return {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    handleError: vi.fn(),
    postWithRetryOnce: actual.postWithRetryOnce,
  };
});

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

  describe('Editor Tab - Review Context Neighbors (Plan J, Phase 6)', () => {
    it('should render a muted context block with before/after neighbor texts when present', () => {
      const item: ReviewItem = {
        item_id: 'review-001',
        character_id: 'char-001',
        character_name: 'Elizabeth',
        confidence: 0.65,
        junction_table: 'speaker',
        related_entity_id: 'span-003',
        neighbors: {
          before: ['Prior line one.', 'Prior line two.'],
          after: ['Next line one.', 'Next line two.'],
        },
      };

      const html = renderReviewItem(item);

      expect(html).toContain('review-neighbors');
      expect(html).toContain('Prior line one.');
      expect(html).toContain('Prior line two.');
      expect(html).toContain('Next line one.');
      expect(html).toContain('Next line two.');
      // Existing card markup must be preserved alongside the context block.
      expect(html).toContain('65% confidence');
      expect(html).toContain('btn-review-accept');
      expect(html).toContain('btn-review-reject');
      expect(html).toContain('btn-review-override');
      expect(html).toContain('data-item-id="review-001"');
    });

    it('should render no context block when neighbors is empty', () => {
      const item: ReviewItem = {
        item_id: 'review-002',
        character_id: 'char-003',
        character_name: 'Unknown Speaker',
        confidence: 0.52,
        junction_table: 'narrator',
        related_entity_id: 'span-002',
        neighbors: { before: [], after: [] },
      };

      const html = renderReviewItem(item);

      expect(html).not.toContain('review-neighbors');
    });

    it('should render no context block when neighbors is undefined (existing fixtures stay valid)', () => {
      // MOCK_REVIEW_ITEMS has no neighbors field - the field is optional.
      const html = renderReviewItem(MOCK_REVIEW_ITEMS[0]);

      expect(html).not.toContain('review-neighbors');
      expect(html).toContain('btn-review-accept');
    });

    it('should render the block when only one side is non-empty', () => {
      const item: ReviewItem = {
        item_id: 'review-003',
        character_id: 'char-001',
        character_name: 'Elizabeth',
        confidence: 0.6,
        junction_table: 'speaker',
        related_entity_id: 'span-001',
        neighbors: { before: ['Only prior line.'], after: [] },
      };

      const html = renderReviewItem(item);

      expect(html).toContain('review-neighbors');
      expect(html).toContain('Only prior line.');
    });

    it('should escape neighbor span text', () => {
      const item: ReviewItem = {
        item_id: 'review-004',
        character_id: 'char-001',
        character_name: 'Elizabeth',
        confidence: 0.6,
        junction_table: 'speaker',
        related_entity_id: 'span-001',
        neighbors: { before: ['<script>alert(1)</script>'], after: [] },
      };

      const html = renderReviewItem(item);

      expect(html).not.toContain('<script>alert(1)</script>');
      expect(html).toContain('&lt;script&gt;');
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

// ---------------------------------------------------------------------------
// Render progress + result surface (Plan F, Phase 1)
//
// Backend contract (app/pipeline/api_export.py render_status — verified,
// unchanged): GET /api/pipeline/render_status/{job_id} returns
//   { job_id, status, output_dir, error, mode }
// where mode ∈ {'individual','batch'}. Individual mode ALSO returns
// total_chunks/completed_chunks/failed_chunks derived from render_chunk rows
// (rows = truth); batch mode has NO per-chunk counts — job-level progress
// only (running → indeterminate animated bar, completed → 100%).
//
// The result surface: #btn-pipeline-download (restored in index.html, wired
// to GET /api/pipeline/download/{job_id} via downloadPipelineRender) and the
// Plan E #btn-pipeline-play-book affordance (GET /api/pipeline/export/audio/
// {job_id} via the singleton player) — both exposed when a render completes.
// ---------------------------------------------------------------------------

describe('Editor Tab — Render Progress + Result Surface (Plan F)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `
      <button id="btn-pipeline-render"></button>
      <button id="btn-pipeline-regen"></button>
      <button id="btn-pipeline-cancel" style="display:none;"></button>
      <button id="btn-pipeline-download" style="display:none;"></button>
      <button id="btn-pipeline-play-book" style="display:none;"></button>
      <div class="progress" style="height: 25px;">
        <div id="full-progress-bar" class="progress-bar progress-bar-striped bg-success" role="progressbar" style="width: 0%">0%</div>
      </div>
      <span id="render-failures-badge" class="badge bg-danger" style="display:none;"></span>
      <span id="pipeline-render-job" class="d-none"></span>
      <div id="spans-table-body"></div>
    `;
  });

  afterEach(async () => {
    state.pipelineRenderJobId = null;
    await cancelPipelineRender(true);
    // Drop leftover mock implementations/Once queues so a failing test cannot
    // leak a render_status payload into later describes: vi.clearAllMocks()
    // keeps implementations, mockReset() clears them (audio-surface describes
    // use the same isolation pattern).
    vi.mocked(API.get).mockReset();
    vi.mocked(API.post).mockReset();
    vi.useRealTimers();
  });

  describe('per-chunk progress (individual mode)', () => {
    it('renders width = completed/total and a chunk-count label from render_status counts', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-ind-1' });
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-ind-1', status: 'running', mode: 'individual',
          output_dir: null, error: null,
          total_chunks: 40, completed_chunks: 12, failed_chunks: 0,
        })
        .mockResolvedValueOnce({
          job_id: 'job-ind-1', status: 'completed', mode: 'individual',
          output_dir: null, error: null,
          total_chunks: 40, completed_chunks: 40, failed_chunks: 0,
        });

      const promise = pipelineRenderAll();
      await vi.advanceTimersByTimeAsync(2000);

      const bar = document.getElementById('full-progress-bar') as HTMLElement;
      expect(bar.style.width).toBe('30%');
      expect(bar.innerText).toBe('12/40 chunks');

      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      // Terminal tick: 100% width, full counts label, no animation class.
      expect(bar.style.width).toBe('100%');
      expect(bar.innerText).toBe('40/40 chunks');
      expect(bar.classList.contains('progress-bar-animated')).toBe(false);
    });

    it('surfaces per-chunk failures as a red count/error badge', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-ind-2' });
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-ind-2', status: 'running', mode: 'individual',
          output_dir: null, error: 'TTS failed on chunk 7',
          total_chunks: 40, completed_chunks: 12, failed_chunks: 3,
        })
        .mockResolvedValueOnce({
          job_id: 'job-ind-2', status: 'completed', mode: 'individual',
          output_dir: null, error: 'TTS failed on chunk 7',
          total_chunks: 40, completed_chunks: 37, failed_chunks: 3,
        });

      const promise = pipelineRenderAll();
      await vi.advanceTimersByTimeAsync(2000);

      const badge = document.getElementById('render-failures-badge') as HTMLElement;
      expect(badge.style.display).toBe('inline-block');
      expect(badge.innerText).toContain('3 chunks failed');
      expect(badge.title).toBe('TTS failed on chunk 7');

      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      // Failures persist on the badge through the terminal tick.
      expect(badge.style.display).toBe('inline-block');
    });

    it('hides the failure badge when no chunks failed', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-ind-3' });
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-ind-3', status: 'running', mode: 'individual',
          output_dir: null, error: null,
          total_chunks: 10, completed_chunks: 4, failed_chunks: 0,
        })
        .mockResolvedValueOnce({
          job_id: 'job-ind-3', status: 'completed', mode: 'individual',
          output_dir: null, error: null,
          total_chunks: 10, completed_chunks: 10, failed_chunks: 0,
        });

      const badge = document.getElementById('render-failures-badge') as HTMLElement;
      badge.style.display = 'inline-block'; // stale from a prior render

      const promise = pipelineRenderAll();
      await vi.advanceTimersByTimeAsync(2000);

      expect(badge.style.display).toBe('none');

      await vi.advanceTimersByTimeAsync(2000);
      await promise;
    });
  });

  describe('job-level progress (batch mode — no per-chunk counts)', () => {
    it('shows an indeterminate animated bar while running and 100% when completed', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-batch-1' });
      vi.mocked(API.get)
        .mockResolvedValueOnce({
          job_id: 'job-batch-1', status: 'running', mode: 'batch',
          output_dir: null, error: null,
        })
        .mockResolvedValueOnce({
          job_id: 'job-batch-1', status: 'completed', mode: 'batch',
          output_dir: null, error: null,
        });

      const promise = pipelineRenderAll();
      await vi.advanceTimersByTimeAsync(2000);

      const bar = document.getElementById('full-progress-bar') as HTMLElement;
      expect(bar.classList.contains('progress-bar-animated')).toBe(true);
      expect(bar.style.width).toBe('100%');
      expect(bar.innerText).toBe('Rendering...');
      // Batch mode carries no per-chunk counts → no failure badge.
      const badge = document.getElementById('render-failures-badge') as HTMLElement;
      expect(badge.style.display).toBe('none');

      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      expect(bar.classList.contains('progress-bar-animated')).toBe(false);
      expect(bar.style.width).toBe('100%');
      expect(bar.innerText).toBe('100%');
    });
  });

  describe('result surface', () => {
    it('restores a reachable #btn-pipeline-download inside the editor tab in index.html', () => {
      const html = readIndexHtml();

      expect(html).toContain('id="btn-pipeline-download"');
      expect(html).toContain('id="render-failures-badge"');
      // The download affordance must live inside the editor tab (reachable),
      // not somewhere in another tab.
      const editorTabStart = html.indexOf('id="editor-tab"');
      const downloadIdx = html.indexOf('id="btn-pipeline-download"');
      expect(editorTabStart).toBeGreaterThan(-1);
      expect(downloadIdx).toBeGreaterThan(editorTabStart);
    });

    it('wires the Download button to GET /api/pipeline/download/{job_id} via downloadPipelineRender', async () => {
      state.pipelineRenderJobId = 'job-dl-1';
      let clickedHref: string | null = null;
      // Spy on the ANCHOR prototype only: downloadPipelineRender creates a
      // temporary <a href="/api/pipeline/download/{id}"> and clicks it, while
      // the button's own click() must still dispatch (buttons share
      // HTMLElement.prototype.click — spying there would swallow the event).
      const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
        clickedHref = this.getAttribute('href');
      });
      try {
        document.body.innerHTML = `<button id="btn-pipeline-download" style="display:none;"></button>`;
        initEditor();
        document.dispatchEvent(new Event('DOMContentLoaded'));

        const btn = document.getElementById('btn-pipeline-download') as HTMLButtonElement;
        expect(btn).not.toBeNull();
        btn.click();

        // downloadPipelineRender runs synchronously up to the anchor click.
        expect(clickSpy).toHaveBeenCalledTimes(1);
        expect(clickedHref).toBe('/api/pipeline/download/job-dl-1');
      } finally {
        clickSpy.mockRestore();
      }
    });

    it('exposes the download button and the Play Book affordance when a render completes', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-done-1' });
      vi.mocked(API.get).mockResolvedValue({
        job_id: 'job-done-1', status: 'completed', mode: 'individual',
        output_dir: null, error: null,
        total_chunks: 40, completed_chunks: 40, failed_chunks: 0,
      });

      const btnDownload = document.getElementById('btn-pipeline-download') as HTMLElement;
      const btnPlayBook = document.getElementById('btn-pipeline-play-book') as HTMLElement;
      expect(btnDownload.style.display).toBe('none');
      expect(btnPlayBook.style.display).toBe('none');

      const promise = pipelineRenderAll();
      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      expect(btnDownload.style.display).toBe('inline-block');
      expect(btnDownload.getAttribute('data-job-id')).toBe('job-done-1');
      expect(btnPlayBook.style.display).toBe('inline-block');
    });

    it('keeps the progress bar neutral (Ready, 0%) when spans load — no static 100% claim', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      const bar = document.getElementById('full-progress-bar') as HTMLElement;
      // Simulate a stale render-complete bar; loadSpans must reset it.
      bar.style.width = '100%';
      bar.innerText = '40/40 chunks';

      await loadSpans();

      expect(bar.style.width).toBe('0%');
      expect(bar.innerText).toBe('Ready');
      expect(bar.innerText).not.toContain('spans loaded');
    });
  });
});

// ---------------------------------------------------------------------------
// Cancel wiring with retry-once (Plan F, Phase 2)
//
// Backend contract (app/pipeline/api_export.py cancel_render — verified,
// unchanged): POST /api/pipeline/cancel_render {job_id} → dict on success; on
// transaction() owner-thread contention (ConcurrentTransactionError) → HTTP
// 503 + Retry-After header. Frontend contract (DD UX workflow #2): the caller
// retries EXACTLY ONCE before surfacing the error. The retry wrapper
// (api.postWithRetryOnce) uses raw fetch, and the api module is a partial
// mock here — so these tests spy on global fetch to count POST attempts and
// to shape 503-then-200 / 503-then-503 sequences.
// ---------------------------------------------------------------------------

describe('Editor Tab — Cancel with Retry-once (Plan F, Phase 2)', () => {
  const originalFetch = globalThis.fetch;

  const retry503 = {
    ok: false,
    status: 503,
    statusText: 'Service Unavailable',
    headers: { get: (name: string) => (name === 'Retry-After' ? '1' : null) },
    json: async () => ({ detail: 'transaction contention' }),
  };
  const noHeader503 = {
    ok: false,
    status: 503,
    statusText: 'Service Unavailable',
    headers: { get: () => null },
    json: async () => ({ detail: 'transaction contention' }),
  };
  const okCancelled = {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => null },
    json: async () => ({ status: 'cancelled', job_id: 'job-cancel-1' }),
  };

  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `
      <button id="btn-pipeline-render"></button>
      <button id="btn-pipeline-regen"></button>
      <button id="btn-pipeline-cancel" style="display:none;"></button>
    `;
    fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy;
    vi.useFakeTimers();
  });

  afterEach(async () => {
    await cancelPipelineRender(true);
    globalThis.fetch = originalFetch;
    vi.mocked(API.get).mockReset();
    vi.mocked(API.post).mockReset();
    vi.useRealTimers();
  });

  it('pipelineCancelRender posts /api/pipeline/cancel_render with {job_id} and retries exactly once on 503+Retry-After, succeeding when the retry returns 200', async () => {
    fetchSpy
      .mockResolvedValueOnce(retry503)
      .mockResolvedValueOnce(okCancelled);

    const promise = pipelineCancelRender('job-cancel-1');
    await vi.advanceTimersByTimeAsync(0); // attempt 1 → 503 → Retry-After delay scheduled
    await vi.advanceTimersByTimeAsync(1000); // delay elapses → attempt 2 → 200

    await expect(promise).resolves.toEqual({ status: 'cancelled', job_id: 'job-cancel-1' });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy).toHaveBeenNthCalledWith(1, '/api/pipeline/cancel_render', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ job_id: 'job-cancel-1' }),
    }));
    expect(fetchSpy).toHaveBeenNthCalledWith(2, '/api/pipeline/cancel_render', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ job_id: 'job-cancel-1' }),
    }));
  });

  it('pipelineCancelRender rejects when both attempts return 503 and makes NO third attempt', async () => {
    fetchSpy.mockResolvedValue(retry503); // both attempts → 503

    const promise = pipelineCancelRender('job-cancel-1');
    const assertion = expect(promise).rejects.toThrow('transaction contention'); // attach handler before advancing
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);

    await assertion;
    expect(fetchSpy).toHaveBeenCalledTimes(2); // exactly 2 attempts — never a 3rd
  });

  it('pipelineCancelRender does NOT retry a 503 without a Retry-After header', async () => {
    fetchSpy.mockResolvedValue(noHeader503);

    const promise = pipelineCancelRender('job-cancel-1');
    const assertion = expect(promise).rejects.toThrow('transaction contention'); // attach handler before advancing
    await vi.advanceTimersByTimeAsync(0);

    await assertion;
    expect(fetchSpy).toHaveBeenCalledTimes(1); // no Retry-After → no retry
  });

  it('cancelPipelineRender() posts /cancel_render for the ACTIVE job and surfaces the failure via the existing catch path when both attempts are 503', async () => {
    vi.mocked(API.post).mockResolvedValue({ job_id: 'job-cancel-1' }); // render start
    vi.mocked(API.get)
      .mockResolvedValueOnce({ job_id: 'job-cancel-1', status: 'running', output_dir: null, error: null })
      .mockResolvedValue({ job_id: 'job-cancel-1', status: 'completed', output_dir: null, error: null });
    fetchSpy.mockResolvedValue(retry503); // both cancel attempts → 503
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const renderPromise = pipelineRenderAll();
    await vi.advanceTimersByTimeAsync(0); // render starts → _currentRenderJobId set

    const cancelPromise = cancelPipelineRender(); // explicit user Cancel (button handler)
    await vi.advanceTimersByTimeAsync(0); // attempt 1 → 503 → delay scheduled
    await vi.advanceTimersByTimeAsync(1000); // delay elapses → attempt 2 → 503 → error surfaced

    // Cleanup FIRST: let the render poll reach terminal state (running →
    // completed) so _currentRenderJobId is cleared even if an assertion below
    // fails (avoids leaking module-private state into later tests).
    await vi.advanceTimersByTimeAsync(2000); // tick 1 → still running
    await vi.advanceTimersByTimeAsync(2000); // tick 2 → completed → render resolves
    await renderPromise;
    await cancelPromise;

    // Exactly 2 POST attempts to /cancel_render with the ACTIVE job id — no 3rd.
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy).toHaveBeenNthCalledWith(1, '/api/pipeline/cancel_render', expect.objectContaining({
      body: JSON.stringify({ job_id: 'job-cancel-1' }),
    }));
    expect(fetchSpy).toHaveBeenNthCalledWith(2, '/api/pipeline/cancel_render', expect.objectContaining({
      body: JSON.stringify({ job_id: 'job-cancel-1' }),
    }));
    // Existing catch path surfaces the failure (cancelPipelineRender logs, it
    // does not toast) — and the buttons are restored.
    expect(consoleErrorSpy).toHaveBeenCalledTimes(1);
    expect(consoleErrorSpy).toHaveBeenCalledWith('Cancel error:', expect.any(Error));
    const btnCancel = document.getElementById('btn-pipeline-cancel');
    const btnRender = document.getElementById('btn-pipeline-render');
    expect(btnCancel?.style.display).toBe('none');
    expect(btnRender?.style.display).toBe('inline-block');

    consoleErrorSpy.mockRestore();
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

// ---------------------------------------------------------------------------
// Export M4B form + cover upload (Plan F, Phase 4)
//
// Backend contract (app/pipeline/api_export.py export_m4b — verified,
// unchanged): POST /api/pipeline/export/m4b is a MULTIPART form route
// (FastAPI Form(...) + File(...) — NOT JSON):
//   job_id (required) + title/author/narrator/year/description (default '')
//   + cover (optional UploadFile).
// Success (200): { status:'ok', output_path, mp3, mp3_path, audacity,
//   audacity_path } with an optional `message` key when libmp3lame is missing
//   (M4B-only degrade — full feature-detect UI is Phase 5; here the message
//   is surfaced as an info alert in the export card).
// Errors: 404 unknown/non-completed job, 410 expired, 409 format mismatch,
//   400 no chunks — the frontend surfaces the backend `detail` via an error
//   toast and keeps the form usable.
//
// The multipart POST MUST go through raw fetch + FormData (the API.post
// wrapper JSON-stringifies and would break the route). FormData inspection in
// jsdom: capture the fetch init.body and iterate [...formData.entries()];
// File values asserted via instanceof File / name.
// ---------------------------------------------------------------------------

describe('Editor Tab — Export M4B (Plan F, Phase 4)', () => {
  const originalFetch = globalThis.fetch;

  const okExport = {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => null },
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: '/data/render_root/book.mp3',
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
    }),
  };
  const okExportM4bOnly = {
    ...okExport,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: false,
      mp3_path: null,
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
      message:
        'MP3 export unavailable: the libmp3lame encoder was not found in this ffmpeg build; exported M4B only.',
    }),
  };
  const errExport = {
    ok: false,
    status: 409,
    statusText: 'Conflict',
    headers: { get: () => null },
    json: async () => ({ detail: 'format mismatch: cover must be jpeg/png' }),
  };

  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.pipelineRenderJobId = 'job-exp-1';
    document.body.innerHTML = `
      <button id="btn-pipeline-render"></button>
      <button id="btn-pipeline-regen"></button>
      <button id="btn-pipeline-cancel" style="display:none;"></button>
      <button id="btn-pipeline-download" style="display:none;"></button>
      <button id="btn-pipeline-play-book" style="display:none;"></button>
      <div class="progress" style="height: 25px;">
        <div id="full-progress-bar" class="progress-bar progress-bar-striped bg-success" role="progressbar" style="width: 0%">0%</div>
      </div>
      <span id="render-failures-badge" class="badge bg-danger" style="display:none;"></span>
      <span id="pipeline-render-job" class="d-none"></span>
      <div id="spans-table-body"></div>
      <div id="export-m4b-card" style="display:none;">
        <form id="export-m4b-form" enctype="multipart/form-data">
          <input type="text" id="export-m4b-title" name="title">
          <input type="text" id="export-m4b-author" name="author">
          <input type="text" id="export-m4b-narrator" name="narrator">
          <input type="text" id="export-m4b-year" name="year">
          <textarea id="export-m4b-description" name="description"></textarea>
          <input type="file" id="export-m4b-cover" name="cover">
          <button type="submit" id="btn-export-m4b">Export M4B</button>
          <div id="export-m4b-info" style="display:none;"></div>
        </form>
      </div>
    `;
    fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy;
  });

  afterEach(async () => {
    state.pipelineRenderJobId = null;
    await cancelPipelineRender(true);
    globalThis.fetch = originalFetch;
    // Drop leftover mock implementations/Once queues so a failing test cannot
    // leak render_status payloads into later describes (Phase 1 isolation
    // pattern: mockReset clears implementations, clearAllMocks does not).
    vi.mocked(API.get).mockReset();
    vi.mocked(API.post).mockReset();
    vi.useRealTimers();
  });

  describe('pipelineExportM4b — multipart FormData shape', () => {
    it('posts job_id + the 5 metadata fields as multipart FormData to /api/pipeline/export/m4b', async () => {
      fetchSpy.mockResolvedValue(okExport);

      const result = await pipelineExportM4b({
        jobId: 'job-exp-1',
        title: 'Pride and Prejudice',
        author: 'Jane Austen',
        narrator: 'Narrator',
        year: '1813',
        description: 'A novel of manners.',
      });

      expect(fetchSpy).toHaveBeenCalledTimes(1);
      const [url, init] = fetchSpy.mock.calls[0];
      expect(url).toBe('/api/pipeline/export/m4b');
      const fd = (init as RequestInit).body as FormData;
      expect(fd).toBeInstanceOf(FormData);
      expect((init as RequestInit).method).toBe('POST');
      // Exactly the 6 string fields — no cover entry when none selected.
      expect([...fd.entries()].map(([k]) => k).sort()).toEqual(
        ['author', 'description', 'job_id', 'narrator', 'title', 'year'],
      );
      expect(fd.get('job_id')).toBe('job-exp-1');
      expect(fd.get('title')).toBe('Pride and Prejudice');
      expect(fd.get('author')).toBe('Jane Austen');
      expect(fd.get('narrator')).toBe('Narrator');
      expect(fd.get('year')).toBe('1813');
      expect(fd.get('description')).toBe('A novel of manners.');
      expect(fd.get('cover')).toBeNull();
      expect(result).toEqual(expect.objectContaining({
        status: 'ok',
        output_path: '/data/render_root/book.m4b',
      }));
    });

    it('appends the cover File when provided and omits the cover entry otherwise', async () => {
      const cover = new File(['fake-jpeg-bytes'], 'cover.jpg', { type: 'image/jpeg' });

      fetchSpy.mockResolvedValue(okExport);
      await pipelineExportM4b({
        jobId: 'job-exp-1', title: 'T', author: 'A', narrator: 'N', year: '', description: '', cover,
      });
      const withCover = (fetchSpy.mock.calls[0][1] as RequestInit).body as FormData;
      expect([...withCover.entries()]).toHaveLength(7); // job_id + 5 fields + cover
      const coverEntry = [...withCover.entries()].find(([k]) => k === 'cover');
      expect(coverEntry).toBeDefined();
      expect(coverEntry?.[1]).toBeInstanceOf(File);
      expect((coverEntry?.[1] as File).name).toBe('cover.jpg');
      expect((coverEntry?.[1] as File).type).toBe('image/jpeg');

      fetchSpy.mockClear();
      await pipelineExportM4b({
        jobId: 'job-exp-1', title: 'T', author: 'A', narrator: 'N', year: '', description: '',
      });
      const noCover = (fetchSpy.mock.calls[0][1] as RequestInit).body as FormData;
      expect([...noCover.entries()]).toHaveLength(6);
      expect([...noCover.entries()].find(([k]) => k === 'cover')).toBeUndefined();
    });
  });

  describe('index.html export form markup', () => {
    it('declares the Export M4B form with the 5 metadata fields, cover file input, submit button, hidden until a render completes', () => {
      const html = readIndexHtml();

      expect(html).toContain('id="export-m4b-form"');
      for (const id of [
        'export-m4b-title',
        'export-m4b-author',
        'export-m4b-narrator',
        'export-m4b-year',
        'export-m4b-description',
        'export-m4b-cover',
      ]) {
        expect(html).toContain(`id="${id}"`);
      }
      expect(html).toContain('id="btn-export-m4b"');
      expect(html).toContain('type="submit"');
      // Idle state: hidden until a render completes (same moment the
      // download/play buttons are revealed).
      expect(html).toMatch(/id="export-m4b-card"[^>]*style="display:\s*none;"/);
      // Reachable: the export card lives inside the editor tab.
      const editorTabStart = html.indexOf('id="editor-tab"');
      const exportIdx = html.indexOf('id="export-m4b-card"');
      expect(editorTabStart).toBeGreaterThan(-1);
      expect(exportIdx).toBeGreaterThan(editorTabStart);
    });
  });

  describe('form submit flow (initEditor wiring)', () => {
    it('submits job_id + form values and reveals the play/download surface with a success toast', async () => {
      const { showToast } = await import('../../src/utils');
      fetchSpy.mockResolvedValue(okExport);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-title') as HTMLInputElement).value = 'Pride and Prejudice';
      (document.getElementById('export-m4b-author') as HTMLInputElement).value = 'Jane Austen';
      (document.getElementById('export-m4b-narrator') as HTMLInputElement).value = 'Narrator';
      (document.getElementById('export-m4b-year') as HTMLInputElement).value = '1813';
      (document.getElementById('export-m4b-description') as HTMLTextAreaElement).value = 'A novel of manners.';

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });
      expect(fetchSpy.mock.calls[0][0]).toBe('/api/pipeline/export/m4b');
      const fd = (fetchSpy.mock.calls[0][1] as RequestInit).body as FormData;
      expect(fd.get('job_id')).toBe('job-exp-1');
      expect(fd.get('title')).toBe('Pride and Prejudice');
      expect(fd.get('author')).toBe('Jane Austen');
      expect(fd.get('narrator')).toBe('Narrator');
      expect(fd.get('year')).toBe('1813');
      expect(fd.get('description')).toBe('A novel of manners.');

      await vi.waitFor(() => {
        expect(showToast).toHaveBeenCalledWith('M4B export complete', 'success');
      });
      // Result surface revealed: play + download enabled for the exported job.
      const btnDownload = document.getElementById('btn-pipeline-download') as HTMLElement;
      const btnPlayBook = document.getElementById('btn-pipeline-play-book') as HTMLElement;
      expect(btnDownload.style.display).toBe('inline-block');
      expect(btnDownload.getAttribute('data-job-id')).toBe('job-exp-1');
      expect(btnPlayBook.style.display).toBe('inline-block');
      // Form reset after success (values cleared).
      expect((document.getElementById('export-m4b-title') as HTMLInputElement).value).toBe('');
    });

    it('surfaces the backend error detail via an error toast and keeps the form usable', async () => {
      const { showToast } = await import('../../src/utils');
      fetchSpy.mockResolvedValue(errExport); // 409 format mismatch
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      const titleInput = document.getElementById('export-m4b-title') as HTMLInputElement;
      titleInput.value = 'Pride and Prejudice';
      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect(showToast).toHaveBeenCalledWith(
          'Export failed: format mismatch: cover must be jpeg/png',
          'error',
        );
      });

      // Form stays usable: values preserved, submit button not disabled.
      expect(titleInput.value).toBe('Pride and Prejudice');
      expect((document.getElementById('btn-export-m4b') as HTMLButtonElement).disabled).toBe(false);
      // No result-surface reveal on failure.
      expect((document.getElementById('btn-pipeline-download') as HTMLElement).style.display).toBe('none');
    });

    it('surfaces the M4B-only message from the response in the export card when present', async () => {
      fetchSpy.mockResolvedValue(okExportM4bOnly);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-m4b-info') as HTMLElement).style.display).toBe('block');
      });
      const info = document.getElementById('export-m4b-info') as HTMLElement;
      expect(info.textContent).toContain('libmp3lame');
      expect(info.textContent).toContain('exported M4B only');
    });
  });

  describe('render lifecycle (card hidden idle, revealed on completion, reset on new render)', () => {
    it('reveals the Export M4B card when a render completes — same moment as play/download', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-exp-r1' });
      vi.mocked(API.get).mockResolvedValue({
        job_id: 'job-exp-r1', status: 'completed', mode: 'individual',
        output_dir: null, error: null, total_chunks: 5, completed_chunks: 5, failed_chunks: 0,
      });

      const card = document.getElementById('export-m4b-card') as HTMLElement;
      expect(card.style.display).toBe('none'); // idle before any render result

      const promise = pipelineRenderAll();
      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      expect(card.style.display).toBe('block');
      const btnDownload = document.getElementById('btn-pipeline-download') as HTMLElement;
      expect(btnDownload.style.display).toBe('inline-block');
      expect(btnDownload.getAttribute('data-job-id')).toBe('job-exp-r1');
    });

    it('hides + resets the form when a new render starts, then reveals it again on completion', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-exp-r2' });
      vi.mocked(API.get).mockResolvedValue({
        job_id: 'job-exp-r2', status: 'completed', mode: 'batch',
        output_dir: null, error: null,
      });

      const card = document.getElementById('export-m4b-card') as HTMLElement;
      const titleInput = document.getElementById('export-m4b-title') as HTMLInputElement;
      card.style.display = 'block'; // stale from a previous completed render
      titleInput.value = 'Stale title';

      const promise = pipelineRenderAll();
      // Render start hides + resets the stale export form synchronously.
      expect(card.style.display).toBe('none');
      expect(titleInput.value).toBe('');

      await vi.advanceTimersByTimeAsync(2000);
      await promise;

      expect(card.style.display).toBe('block'); // revealed again on completion
    });

    it('warns and does not POST when no completed render job exists', async () => {
      const { showToast } = await import('../../src/utils');
      state.pipelineRenderJobId = null;
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-title') as HTMLInputElement).value = 'No job';
      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect(showToast).toHaveBeenCalledWith(
          'No completed render to export. Render the audiobook first.',
          'warning',
        );
      });
      expect(fetchSpy).not.toHaveBeenCalled();
    });
  });
});

// ---------------------------------------------------------------------------
// Export capability affordances — MP3 + Audacity ZIP_STORED (Plan F, Phase 5)
//
// Feature-detect contract (app/pipeline/api_export.py export_m4b — verified,
// unchanged): the /export/m4b response IS the capability carrier — there is no
// separate capability endpoint:
//   mp3: true|false          (true when the libmp3lame encoder is present)
//   mp3_path: '<path>'|null  (the run-dir audiobook.mp3, null when absent)
//   audacity: true           (the ZIP_STORED bundle is always producible)
//   audacity_path: '<path>'  (the run-dir audiobook-audacity.zip)
//   message: '<str>'         (present ONLY when mp3=false — M4B-only degrade)
// The affordances are rendered from these flags after a successful export:
// mp3=true → MP3 download link visible; audacity=true → Audacity bundle link
// visible; mp3=false → MP3 link suppressed + the M4B-only message surfaced
// (response.message, or the frontend default 'MP3 export unavailable —
// M4B-only' when the key is absent). The affordances reset when a new render
// starts (hideExportM4bForm) so a stale capability row from a previous job
// never lingers.
//
// URL scheme (Plan K): the hrefs are same-origin serving routes built from
// the job id — /api/pipeline/export/mp3/{job_id} and
// /api/pipeline/export/audacity/{job_id} — served by api_export.py via
// FileResponse404. The artifact paths in the response (mp3_path /
// audacity_path) are server-side filesystem locations and never reach the
// browser; they still gate visibility exactly as before (a link is shown iff
// its flag AND its path are present — the path is the capability signal, not
// the href), so a suppressed link carries no route at all. In jsdom,
// anchor.href resolves against the document base URL — assert the RAW
// attribute via getAttribute('href').
// ---------------------------------------------------------------------------

describe('Editor Tab — Export Capabilities MP3/Audacity (Plan F, Phase 5)', () => {
  const originalFetch = globalThis.fetch;

  const okExportFull = {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: { get: () => null },
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: '/data/render_root/book.mp3',
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
    }),
  };
  const okExportM4bOnly = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: false,
      mp3_path: null,
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
      message:
        'MP3 export unavailable: the libmp3lame encoder was not found in this ffmpeg build; exported M4B only.',
    }),
  };
  // mp3=false with NO message key — the frontend must supply the degrade copy.
  const okExportM4bOnlyNoMessage = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: false,
      mp3_path: null,
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
    }),
  };
  // audacity=false — feature-detect is per-flag; the bundle link must hide.
  const okExportNoAudacity = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: '/data/render_root/book.mp3',
      audacity: false,
      audacity_path: null,
    }),
  };
  // mp3=true but mp3_path=null — the gating is (mp3 && mp3_path): the path is
  // the capability signal, so the MP3 affordance must hide even though the
  // serving route IS constructible from the job id (Plan K: hidden regardless
  // of route).
  const okExportMp3NoPath = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: null,
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
    }),
  };
  // Plan L pause-assembly tri-state fixtures. pauses_state 'applied' → the
  // canonical paused artifact was used (resolved values + override count + the
  // backend pauses_message render in #export-pauses-info); 'failed' → bounded
  // pauses_error (assembly unavailable; unpaused fallback exported); absent →
  // no pause surface. The legacy pauses_applied:true flag still satisfies the
  // 'applied' branch for backward-compatible responses.
  const okExportPausesApplied = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: '/data/render_root/book.mp3',
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
      pauses_applied: true,
      pauses_state: 'applied',
      pauses_message: 'Pauses applied: the exported audio includes the resolved speaker pauses between spans.',
      resolved_pause_between_speakers_ms: 600,
      resolved_pause_same_speaker_ms: 300,
      pause_override_count: 2,
    }),
  };
  const okExportPausesAppliedNoMessage = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: '/data/render_root/book.mp3',
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
      pauses_applied: true,
      pauses_state: 'applied',
      resolved_pause_between_speakers_ms: 500,
      resolved_pause_same_speaker_ms: 250,
      pause_override_count: 1,
    }),
  };
  const okExportPauseFailed = {
    ...okExportFull,
    json: async () => ({
      status: 'ok',
      output_path: '/data/render_root/book.m4b',
      mp3: true,
      mp3_path: '/data/render_root/book.mp3',
      audacity: true,
      audacity_path: '/data/render_root/audiobook-audacity.zip',
      pauses_applied: false,
      pauses_state: 'failed',
      pauses_error: 'Paused audio assembly artifact not found; exported the concatenated source audio without inserted pauses.',
      resolved_pause_between_speakers_ms: 500,
      resolved_pause_same_speaker_ms: 250,
      pause_override_count: 1,
    }),
  };

  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.pipelineRenderJobId = 'job-cap-1';
    document.body.innerHTML = `
      <button id="btn-pipeline-render"></button>
      <button id="btn-pipeline-regen"></button>
      <button id="btn-pipeline-cancel" style="display:none;"></button>
      <button id="btn-pipeline-download" style="display:none;"></button>
      <button id="btn-pipeline-play-book" style="display:none;"></button>
      <div class="progress" style="height: 25px;">
        <div id="full-progress-bar" class="progress-bar progress-bar-striped bg-success" role="progressbar" style="width: 0%">0%</div>
      </div>
      <span id="render-failures-badge" class="badge bg-danger" style="display:none;"></span>
      <span id="pipeline-render-job" class="d-none"></span>
      <div id="spans-table-body"></div>
      <div id="export-m4b-card" style="display:none;">
        <form id="export-m4b-form" enctype="multipart/form-data">
          <input type="text" id="export-m4b-title" name="title">
          <input type="text" id="export-m4b-author" name="author">
          <input type="text" id="export-m4b-narrator" name="narrator">
          <input type="text" id="export-m4b-year" name="year">
          <textarea id="export-m4b-description" name="description"></textarea>
          <input type="file" id="export-m4b-cover" name="cover">
          <button type="submit" id="btn-export-m4b">Export M4B</button>
          <div id="export-m4b-info" style="display:none;"></div>
          <div id="export-pauses-info" style="display:none;"></div>
        </form>
        <a id="export-mp3-link" href="#" download style="display:none;">Download MP3</a>
        <a id="export-audacity-link" href="#" download style="display:none;">Download Audacity bundle</a>
      </div>
    `;
    fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy;
  });

  afterEach(async () => {
    state.pipelineRenderJobId = null;
    await cancelPipelineRender(true);
    globalThis.fetch = originalFetch;
    vi.mocked(API.get).mockReset();
    vi.mocked(API.post).mockReset();
    vi.useRealTimers();
  });

  describe('index.html capability markup', () => {
    it('declares the MP3 + Audacity download links inside the export card, hidden by default', () => {
      const html = readIndexHtml();

      expect(html).toContain('id="export-mp3-link"');
      expect(html).toContain('id="export-audacity-link"');
      // Plan K pause capability disclosure element (dedicated, distinct from
      // the M4B-only degrade message).
      expect(html).toContain('id="export-pauses-info"');
      expect(html).toMatch(/id="export-pauses-info"[^>]*style="display:\s*none;"/);
      // Idle state: both affordances hidden (capability row appears only after
      // a successful export reports support).
      expect(html).toMatch(/id="export-mp3-link"[^>]*style="display:\s*none;"/);
      expect(html).toMatch(/id="export-audacity-link"[^>]*style="display:\s*none;"/);
      // Inside the export card (a capability of the export flow, not a
      // standalone surface).
      const exportIdx = html.indexOf('id="export-m4b-card"');
      const mp3Idx = html.indexOf('id="export-mp3-link"');
      const audacityIdx = html.indexOf('id="export-audacity-link"');
      expect(exportIdx).toBeGreaterThan(-1);
      expect(mp3Idx).toBeGreaterThan(exportIdx);
      expect(audacityIdx).toBeGreaterThan(exportIdx);
    });
  });

  describe('capability rendering from the export response (submit flow)', () => {
    it('shows MP3 + Audacity download links with the serving routes when the backend reports both (mp3:true, audacity:true)', async () => {
      fetchSpy.mockResolvedValue(okExportFull);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-mp3-link') as HTMLElement).style.display).toBe('inline-block');
      });
      const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement;
      const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement;
      // URL scheme pinned: href = the serving route built from the job id —
      // the response artifact path is a server-side location, never exposed.
      expect(mp3.getAttribute('href')).toBe('/api/pipeline/export/mp3/job-cap-1');
      expect(audacity.style.display).toBe('inline-block');
      expect(audacity.getAttribute('href')).toBe('/api/pipeline/export/audacity/job-cap-1');
      // mp3=true → no degrade message.
      expect((document.getElementById('export-m4b-info') as HTMLElement).style.display).toBe('none');
    });

    it('suppresses the MP3 affordance and surfaces the M4B-only response message when mp3:false (libmp3lame missing)', async () => {
      fetchSpy.mockResolvedValue(okExportM4bOnly);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-m4b-info') as HTMLElement).style.display).toBe('block');
      });
      const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement;
      const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement;
      // M4B-only degrade: MP3 suppressed, Audacity still offered. The
      // suppressed link carries NO route — the serving route is never applied
      // to a link hidden by the flag gating (Plan K: hidden regardless of
      // route).
      expect(mp3.style.display).toBe('none');
      expect(mp3.getAttribute('href')).toBe('');
      expect(audacity.style.display).toBe('inline-block');
      expect(audacity.getAttribute('href')).toBe('/api/pipeline/export/audacity/job-cap-1');
      const info = document.getElementById('export-m4b-info') as HTMLElement;
      expect(info.textContent).toContain('libmp3lame');
      expect(info.textContent).toContain('exported M4B only');
    });

    it('shows the frontend default M4B-only message when mp3:false and the response carries no message key', async () => {
      fetchSpy.mockResolvedValue(okExportM4bOnlyNoMessage);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-m4b-info') as HTMLElement).style.display).toBe('block');
      });
      const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement;
      expect(mp3.style.display).toBe('none');
      expect((document.getElementById('export-m4b-info') as HTMLElement).textContent).toBe(
        'MP3 export unavailable — M4B-only',
      );
    });

    it('hides the Audacity affordance when audacity:false (feature-detect is per-flag)', async () => {
      fetchSpy.mockResolvedValue(okExportNoAudacity);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        // Positive condition — the handler sets MP3 visible only after the
        // export resolves and the capability row renders (a wait on the
        // audacity 'none' would pass trivially from the fixture default).
        expect((document.getElementById('export-mp3-link') as HTMLElement).style.display).toBe('inline-block');
      });
      const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement;
      expect(audacity.style.display).toBe('none');
      const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement;
      expect(mp3.getAttribute('href')).toBe('/api/pipeline/export/mp3/job-cap-1');
    });

    it('hides the MP3 link with no route when the flag gating fails (mp3:true but mp3_path:null) — route availability never overrides the capability signal', async () => {
      fetchSpy.mockResolvedValue(okExportMp3NoPath);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-audacity-link') as HTMLElement).style.display).toBe('inline-block');
      });
      const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement;
      const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement;
      // Gating is (mp3 && mp3_path): the path signal is absent → the link
      // stays hidden AND carries no href, even though the job id could build
      // a serving route.
      expect(mp3.style.display).toBe('none');
      expect(mp3.getAttribute('href')).toBe('');
      // The audacity affordance is unaffected (route applied).
      expect(audacity.getAttribute('href')).toBe('/api/pipeline/export/audacity/job-cap-1');
    });

    it('surfaces the pause-assembly tri-state when pauses_state:applied — resolved values + override count render in #export-pauses-info', async () => {
      fetchSpy.mockResolvedValue(okExportPausesApplied);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-pauses-info') as HTMLElement).style.display).toBe('block');
      });
      const pauseInfo = document.getElementById('export-pauses-info') as HTMLElement;
      expect(pauseInfo.textContent).toContain('600 ms between speakers');
      expect(pauseInfo.textContent).toContain('300 ms same speaker');
      expect(pauseInfo.textContent).toContain('2 span overrides');
      // The backend pauses_message is appended to the resolved-value line.
      expect(pauseInfo.textContent).toContain('resolved speaker pauses');
      // The MP3/Audacity affordances render normally alongside the pause surface.
      expect((document.getElementById('export-mp3-link') as HTMLElement).style.display).toBe('inline-block');
      expect((document.getElementById('export-m4b-info') as HTMLElement).style.display).toBe('none');
    });

    it('shows the resolved-value fallback line when pauses_state:applied but the response carries no pauses_message', async () => {
      fetchSpy.mockResolvedValue(okExportPausesAppliedNoMessage);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-pauses-info') as HTMLElement).style.display).toBe('block');
      });
      const pauseInfo = document.getElementById('export-pauses-info') as HTMLElement;
      expect(pauseInfo.textContent).toContain('Pauses applied: 500 ms between speakers');
      expect(pauseInfo.textContent).toContain('250 ms same speaker');
      expect(pauseInfo.textContent).toContain('1 span override');
    });

    it('surfaces the bounded pauses_error when pauses_state:failed — assembly unavailable', async () => {
      fetchSpy.mockResolvedValue(okExportPauseFailed);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-pauses-info') as HTMLElement).style.display).toBe('block');
      });
      const pauseInfo = document.getElementById('export-pauses-info') as HTMLElement;
      expect(pauseInfo.textContent).toContain('Pause assembly unavailable:');
      expect(pauseInfo.textContent).toContain('without inserted pauses');
    });

    it('shows no pause disclosure when the response omits pauses_applied (undefined — not === false)', async () => {
      fetchSpy.mockResolvedValue(okExportFull);
      initEditor();
      document.dispatchEvent(new Event('DOMContentLoaded'));

      (document.getElementById('export-m4b-form') as HTMLFormElement)
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

      await vi.waitFor(() => {
        expect((document.getElementById('export-mp3-link') as HTMLElement).style.display).toBe('inline-block');
      });
      expect((document.getElementById('export-pauses-info') as HTMLElement).style.display).toBe('none');
    });
  });

  describe('render lifecycle (stale capability reset)', () => {
    it('hides + clears the capability links when a new render starts — no stale buttons from a previous export', async () => {
      vi.useFakeTimers();
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-cap-r1' });
      vi.mocked(API.get).mockResolvedValue({
        job_id: 'job-cap-r1', status: 'completed', mode: 'individual',
        output_dir: null, error: null, total_chunks: 5, completed_chunks: 5, failed_chunks: 0,
      });

      // Stale capability state from a previous completed export.
      const mp3 = document.getElementById('export-mp3-link') as HTMLAnchorElement;
      const audacity = document.getElementById('export-audacity-link') as HTMLAnchorElement;
      mp3.style.display = 'inline-block';
      mp3.setAttribute('href', '/data/render_root/stale.mp3');
      audacity.style.display = 'inline-block';
      audacity.setAttribute('href', '/data/render_root/stale-audacity.zip');
      (document.getElementById('export-m4b-card') as HTMLElement).style.display = 'block';
      (document.getElementById('export-m4b-info') as HTMLElement).style.display = 'block';
      (document.getElementById('export-m4b-info') as HTMLElement).textContent = 'stale message';
      (document.getElementById('export-pauses-info') as HTMLElement).style.display = 'block';
      (document.getElementById('export-pauses-info') as HTMLElement).textContent = 'stale pause disclosure';

      const promise = pipelineRenderAll();
      // New render start resets the stale capability row synchronously.
      expect(mp3.style.display).toBe('none');
      expect(mp3.getAttribute('href')).toBe('');
      expect(audacity.style.display).toBe('none');
      expect(audacity.getAttribute('href')).toBe('');
      expect((document.getElementById('export-m4b-info') as HTMLElement).style.display).toBe('none');
      // The pause disclosure is cleared too — no stale limitation message.
      expect((document.getElementById('export-pauses-info') as HTMLElement).style.display).toBe('none');
      expect((document.getElementById('export-pauses-info') as HTMLElement).textContent).toBe('');

      await vi.advanceTimersByTimeAsync(2000);
      await promise;
    });
  });
});

// ---------------------------------------------------------------------------
// Editor Tab — Per-Span Pause Override (Plan L, Phase 5)
// ---------------------------------------------------------------------------
// Each span row exposes a bounded number input (0..10000 ms). The change
// handler persists via PUT /api/pipeline/span/{id}/pause_after, mapping
// blank → null (clear override → resolve the applicable default) and explicit
// 0 → intentional no-gap. The value is enriched on load via GET.

describe('Editor Tab — Per-Span Pause Override (Plan L, Phase 5)', () => {
  const rawWithIds = [
    { id: 'span-1', speaker: 'Narrator', text: 'Once upon a time', instruct: 'calm' },
    { id: 'span-2', speaker: 'Lizzy', text: 'Indeed', instruct: 'brisk' },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(API.get).mockReset();
    vi.mocked(API.put).mockReset();
    state.pipelineBookId = 'book-123';
    document.body.innerHTML = `<div id="spans-table-body"></div><div id="full-progress-bar"></div>`;
  });

  afterEach(() => {
    state.pipelineBookId = null;
  });

  it('GET /span/{id}/pause_after returns the persisted pause override', async () => {
    vi.mocked(API.get).mockResolvedValue({ pause_after_ms: 800 });
    const res = await pipelineGetSpanPause('span-1');
    expect(API.get).toHaveBeenCalledWith('/api/pipeline/span/span-1/pause_after');
    expect(res.pause_after_ms).toBe(800);
  });

  it('PUT /span/{id}/pause_after persists a positive pause override', async () => {
    vi.mocked(API.put).mockResolvedValue({ status: 'ok' });
    await pipelineSetSpanPause('span-1', 800);
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/span/span-1/pause_after', { pause_after_ms: 800 });
  });

  it('PUT /span/{id}/pause_after with null clears the override (resolve default)', async () => {
    vi.mocked(API.put).mockResolvedValue({ status: 'ok' });
    await pipelineSetSpanPause('span-1', null);
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/span/span-1/pause_after', { pause_after_ms: null });
  });

  it('enriches each span pause on load — unset spans render an empty (default) input', async () => {
    vi.mocked(API.get).mockResolvedValue(rawWithIds); // export + enrichment GETs both resolve to raw
    await loadSpans();
    const input = document.querySelector<HTMLInputElement>('.span-pause[data-span-id="span-1"]');
    expect(input).toBeTruthy();
    // Unset → empty value + placeholder 'default' (never '0', which is no-gap).
    expect(input!.value).toBe('');
    expect(input!.placeholder).toBe('default');
  });

  it('persists a committed positive value via PUT and mirrors it into the cache', async () => {
    vi.mocked(API.get).mockResolvedValue(rawWithIds);
    document.dispatchEvent(new Event('DOMContentLoaded')); // wire spansTableBody change listener
    await loadSpans();
    const input = document.querySelector<HTMLInputElement>('.span-pause[data-span-id="span-1"]')!;
    input.value = '800';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() => {
      expect(API.put).toHaveBeenCalledWith('/api/pipeline/span/span-1/pause_after', { pause_after_ms: 800 });
    });
    // The handler mirrors the value into the cache AFTER the PUT resolves.
    await vi.waitFor(() => {
      expect(getCachedSpans().find(s => s.global_index === 1)?.pause_after_ms).toBe(800);
    });
  });

  it('persists null (resolve default) when the committed input is blank — 0 is never conflated with empty', async () => {
    vi.mocked(API.get).mockResolvedValue(rawWithIds);
    document.dispatchEvent(new Event('DOMContentLoaded'));
    await loadSpans();
    const input = document.querySelector<HTMLInputElement>('.span-pause[data-span-id="span-1"]')!;
    input.value = '';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() => {
      expect(API.put).toHaveBeenCalledWith('/api/pipeline/span/span-1/pause_after', { pause_after_ms: null });
    });
  });

  it('rejects an out-of-bounds value client-side — no PUT is issued and the input reverts', async () => {
    vi.mocked(API.get).mockResolvedValue(rawWithIds);
    document.dispatchEvent(new Event('DOMContentLoaded'));
    await loadSpans();
    const input = document.querySelector<HTMLInputElement>('.span-pause[data-span-id="span-1"]')!;
    input.value = '15000';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() => {
      // Reverted to the cached value (empty = unset) after the rejection.
      expect(input.value).toBe('');
    });
    expect(API.put).not.toHaveBeenCalled();
  });
});


// The toggle is a UI write of book.single_speaker (CONTRACTS decision #9):
// enforcement happens ONLY at the render boundary (tts_integration
// _enforce_single_speaker); the script stays faithful. The toggle must write
// the flag through the backend path, reflect the saved value on load, and
// revert + surface an error when a write fails.

describe('Editor Tab — Single-Speaker Toggle (Plan J, Phase 2)', () => {
  beforeEach(() => {
    // mockReset (not clearAllMocks): clears any unconsumed mockResolvedValueOnce
    // queue that an earlier test in this file may have left on API.get/API.put,
    // so each test here is hermetic (see exec-worker log L139 convention).
    vi.mocked(API.get).mockReset();
    vi.mocked(API.put).mockReset();
    state.pipelineBookId = 'book-123';
  });

  afterEach(() => {
    state.pipelineBookId = null;
  });

  it('pipelineSetSingleSpeaker writes the flag through the backend path', async () => {
    await pipelineSetSingleSpeaker('book-123', true);

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/book/book-123/single_speaker', {
      single_speaker: true,
    });
  });

  it('pipelineSetSingleSpeaker persists the off state', async () => {
    await pipelineSetSingleSpeaker('book-123', false);

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/book/book-123/single_speaker', {
      single_speaker: false,
    });
  });

  it('pipelineGetSingleSpeaker reads the saved flag via GET', async () => {
    vi.mocked(API.get).mockResolvedValue({ single_speaker: 1 });

    const enabled = await pipelineGetSingleSpeaker('book-123');

    expect(API.get).toHaveBeenCalledWith('/api/pipeline/book/book-123/single_speaker');
    expect(enabled).toBe(true);
  });

  it('pipelineGetSingleSpeaker maps the off column value to false', async () => {
    vi.mocked(API.get).mockResolvedValue({ single_speaker: 0 });

    await expect(pipelineGetSingleSpeaker('book-123')).resolves.toBe(false);
  });

  it('loadSingleSpeakerToggle reflects the saved value on load', async () => {
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    vi.mocked(API.get).mockResolvedValue({ single_speaker: 1 });

    await loadSingleSpeakerToggle();

    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    expect(toggle.checked).toBe(true);
  });

  it('loadSingleSpeakerToggle defaults off when the saved value is 0', async () => {
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    vi.mocked(API.get).mockResolvedValue({ single_speaker: 0 });

    await loadSingleSpeakerToggle();

    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    expect(toggle.checked).toBe(false);
  });

  it('loadSingleSpeakerToggle defaults off and surfaces an error when the read fails', async () => {
    const { showToast } = await import('../../src/utils');
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    vi.mocked(API.get).mockRejectedValue(new Error('boom'));

    await loadSingleSpeakerToggle();

    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    expect(showToast).toHaveBeenCalledWith('Failed to load single-speaker setting', 'error');
  });

  it('loadSingleSpeakerToggle stays off and does not fetch without a book', async () => {
    state.pipelineBookId = null;
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle" checked>`;

    await loadSingleSpeakerToggle();

    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    expect(API.get).not.toHaveBeenCalled();
  });

  it('handleSingleSpeakerToggleChange writes the new value and keeps the toggle', async () => {
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    toggle.checked = true;

    await handleSingleSpeakerToggleChange();

    expect(API.put).toHaveBeenCalledWith('/api/pipeline/book/book-123/single_speaker', {
      single_speaker: true,
    });
    expect(toggle.checked).toBe(true);
  });

  it('handleSingleSpeakerToggleChange reverts the toggle and surfaces an error on write failure', async () => {
    const { showToast } = await import('../../src/utils');
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    toggle.checked = true;
    vi.mocked(API.put).mockRejectedValueOnce(new Error('server down'));

    await handleSingleSpeakerToggleChange();

    expect(toggle.checked).toBe(false);
    expect(showToast).toHaveBeenCalledWith('Failed to save single-speaker setting', 'error');
  });

  it('wires the toggle change event to the backend write via initEditor', async () => {
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    initEditor();
    document.dispatchEvent(new Event('DOMContentLoaded'));

    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));

    await vi.waitFor(() => {
      expect(API.put).toHaveBeenCalledWith('/api/pipeline/book/book-123/single_speaker', {
        single_speaker: true,
      });
    });
  });

  it('wires the toggle load into the editor tab-switch handler via initEditor', async () => {
    // Full editor fixture: the tab-switch handler calls loadSpans + loadReviewItems
    // + loadSingleSpeakerToggle; all three DOM surfaces must exist so each makes
    // its own API.get call.
    document.body.innerHTML = `
      <a class="nav-link" data-tab="editor"></a>
      <div id="spans-table-body"></div>
      <div id="review-items-container"></div>
      <input type="checkbox" id="single-speaker-toggle">
    `;
    initEditor();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    // URL-dispatch mock: order/count-agnostic so the handler's exact call
    // sequence can never leak a mockResolvedValueOnce into a later test.
    vi.mocked(API.get).mockImplementation(async (endpoint: string) => {
      if (endpoint === '/api/pipeline/book/book-123/single_speaker') {
        return { single_speaker: 1 };
      }
      if (endpoint === '/api/pipeline/export/book-123') {
        return MOCK_SPANS_RAW;
      }
      if (endpoint === '/api/pipeline/review/book-123') {
        return MOCK_REVIEW_ITEMS;
      }
      return { single_speaker: 0 };
    });

    const editorLink = document.querySelector('[data-tab="editor"]') as HTMLElement;
    editorLink.click();

    await vi.waitFor(() => {
      const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;
      expect(toggle.checked).toBe(true);
    });
  });

  it('round-trips: a saved value reflects on the next load', async () => {
    document.body.innerHTML = `<input type="checkbox" id="single-speaker-toggle">`;
    const toggle = document.getElementById('single-speaker-toggle') as HTMLInputElement;

    // Save: toggle on → backend write
    toggle.checked = true;
    await handleSingleSpeakerToggleChange();
    expect(API.put).toHaveBeenCalledWith('/api/pipeline/book/book-123/single_speaker', {
      single_speaker: true,
    });

    // Reload: backend returns the persisted flag → toggle reflects it
    vi.mocked(API.get).mockResolvedValue({ single_speaker: 1 });
    toggle.checked = false;
    await loadSingleSpeakerToggle();
    expect(toggle.checked).toBe(true);
  });

  it('index.html exposes the toggle with the contract label and tooltip inside the editor tab', () => {
    const html = readIndexHtml();

    expect(html).toContain('id="single-speaker-toggle"');
    expect(html).toContain('>Single-speaker render</label>');
    expect(html).toContain('forces NARRATOR at render; script stays faithful');
    const editorTabStart = html.indexOf('id="editor-tab"');
    const toggleIdx = html.indexOf('id="single-speaker-toggle"');
    expect(editorTabStart).toBeGreaterThan(-1);
    expect(toggleIdx).toBeGreaterThan(editorTabStart);
  });
});

// ---------------------------------------------------------------------------
// Span-text Undo (Plan J, Phase 3)
//
// Design (DD UX workflow #7 + evidence trail): undo = in-memory stack of
// {spanId, priorValue} pushed on each SUCCESSFUL span-text edit (focusout →
// PUT /span/{id}/text). The Undo button pops the most recent entry and reverts
// through the SAME server-validated pipelineUpdateSpanText path — no
// audit-journal/replay mechanism (explicitly rejected in the DD evidence
// trail). The stack clears on snapshot load (projects.ts loadProject), at
// render start (pipelineRenderAll), and whenever spans (re)load — the
// book-switch/tab-switch choke point.
// ---------------------------------------------------------------------------

describe('Editor Tab — Span-Text Undo (Plan J, Phase 3)', () => {
  // Spans MUST carry server ids for the focusout handler to act on them
  // (data-span-id); the file-level MOCK_SPANS_RAW omits ids.
  const MOCK_SPANS_WITH_IDS_RAW = [
    { id: 'span-1', speaker: 'Narrator', text: 'It was a dark and stormy night.', instruct: 'dramatic' },
    { id: 'span-2', speaker: 'Elizabeth', text: 'I cannot believe it!', instruct: 'surprised' },
  ];

  beforeEach(() => {
    // mockReset (not clearAllMocks): clears any unconsumed mockResolvedValueOnce
    // queue left on API.get/API.put/API.post by an earlier test in this file,
    // so each test here is hermetic (exec-worker log L180 convention).
    vi.mocked(API.get).mockReset();
    vi.mocked(API.put).mockReset();
    vi.mocked(API.post).mockReset();
    state.pipelineBookId = 'book-123';
  });

  afterEach(() => {
    state.pipelineBookId = null;
  });

  it('editing a span then clicking Undo reverts to the prior text via PUT', async () => {
    document.body.innerHTML = `
      <div id="spans-table-body"></div>
      <button id="btn-pipeline-undo" disabled></button>
    `;
    initEditor();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_WITH_IDS_RAW);
    vi.mocked(API.put).mockResolvedValue({ status: 'ok', span_id: 'span-1' });

    await loadSpans();

    // Edit the first span through the delegated focusout handler.
    const cell = document.querySelector('.span-text') as HTMLElement;
    cell.textContent = 'Edited line.';
    cell.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    // Wait for BOTH the PUT and the undo push to land (the push runs in the
    // microtask after the PUT resolves) before clicking — a disabled button
    // swallows clicks in jsdom.
    await vi.waitFor(() => {
      expect(API.put).toHaveBeenCalledWith('/api/pipeline/span/span-1/text', { text: 'Edited line.' });
      expect((document.getElementById('btn-pipeline-undo') as HTMLButtonElement).disabled).toBe(false);
    });

    // Undo → the SAME PUT path reverts to the PRIOR value. Wait for BOTH the
    // PUT and the visible-row restore (the restore runs in the microtask after
    // the PUT resolves — poll the DOM state, the P2 convention).
    const btnUndo = document.getElementById('btn-pipeline-undo') as HTMLButtonElement;
    btnUndo.click();
    await vi.waitFor(() => {
      expect(API.put).toHaveBeenLastCalledWith('/api/pipeline/span/span-1/text', {
        text: 'It was a dark and stormy night.',
      });
      expect(document.querySelector('.span-text')?.textContent).toBe('It was a dark and stormy night.');
    });
  });

  it('shows a success toast when an undo succeeds', async () => {
    const { showToast } = await import('../../src/utils');
    document.body.innerHTML = `<button id="btn-pipeline-undo" disabled></button>`;
    vi.mocked(API.put).mockResolvedValue({ status: 'ok', span_id: 'span-1' });

    pushUndoEntry('span-1', 'It was a dark and stormy night.');
    await undoLastSpanEdit();

    expect(showToast).toHaveBeenCalledWith('Undo: span text reverted', 'success');
  });

  it('keeps the Undo button disabled until an edit pushes, then disabled again after undo', async () => {
    document.body.innerHTML = `
      <div id="spans-table-body"></div>
      <button id="btn-pipeline-undo" disabled></button>
    `;
    initEditor();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_WITH_IDS_RAW);
    vi.mocked(API.put).mockResolvedValue({ status: 'ok', span_id: 'span-1' });
    await loadSpans();

    const btnUndo = document.getElementById('btn-pipeline-undo') as HTMLButtonElement;
    expect(btnUndo.disabled).toBe(true);

    // A successful span-text edit enables the button.
    const cell = document.querySelector('.span-text') as HTMLElement;
    cell.textContent = 'Edited line.';
    cell.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    await vi.waitFor(() => {
      expect(btnUndo.disabled).toBe(false);
    });

    // Undoing the only entry disables it again (stack empty).
    btnUndo.click();
    await vi.waitFor(() => {
      expect(btnUndo.disabled).toBe(true);
    });
  });

  it('pushUndoEntry/clearUndoStack maintain the stack and the button state', async () => {
    document.body.innerHTML = `<button id="btn-pipeline-undo" disabled></button>`;
    const btn = document.getElementById('btn-pipeline-undo') as HTMLButtonElement;

    expect(getUndoStack()).toEqual([]);
    expect(btn.disabled).toBe(true);

    pushUndoEntry('span-1', 'old one');
    pushUndoEntry('span-2', 'old two');
    expect(getUndoStack()).toEqual([
      { spanId: 'span-1', priorValue: 'old one' },
      { spanId: 'span-2', priorValue: 'old two' },
    ]);
    expect(btn.disabled).toBe(false);

    clearUndoStack();
    expect(getUndoStack()).toEqual([]);
    expect(btn.disabled).toBe(true);
  });

  it('clears the undo stack when a render starts (pipelineRenderAll)', async () => {
    vi.mocked(API.post).mockResolvedValue({ job_id: 'job-abc-123' });
    vi.mocked(API.get).mockResolvedValue({ job_id: 'job-abc-123', status: 'completed', output_dir: null, error: null });

    pushUndoEntry('span-1', 'old');
    expect(getUndoStack()).toHaveLength(1);

    vi.useFakeTimers();
    try {
      const promise = pipelineRenderAll();
      // The stack clears synchronously at render start, before any await.
      expect(getUndoStack()).toEqual([]);

      await vi.advanceTimersByTimeAsync(2000);
      await promise;
    } finally {
      await cancelPipelineRender(true);
      vi.useRealTimers();
    }
  });

  it('clears the undo stack when spans (re)load (book-switch choke point)', async () => {
    document.body.innerHTML = `
      <div id="spans-table-body"></div>
      <button id="btn-pipeline-undo" disabled></button>
    `;
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_WITH_IDS_RAW);

    pushUndoEntry('span-1', 'old');
    expect(getUndoStack()).toHaveLength(1);

    await loadSpans();

    expect(getUndoStack()).toEqual([]);
    const btn = document.getElementById('btn-pipeline-undo') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('does not push an undo entry for an empty-text edit', async () => {
    const { showToast } = await import('../../src/utils');
    document.body.innerHTML = `
      <div id="spans-table-body"></div>
      <button id="btn-pipeline-undo" disabled></button>
    `;
    initEditor();
    document.dispatchEvent(new Event('DOMContentLoaded'));
    vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_WITH_IDS_RAW);
    await loadSpans();

    const cell = document.querySelector('.span-text') as HTMLElement;
    cell.textContent = '   ';
    cell.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));

    expect(showToast).toHaveBeenCalledWith('Span text cannot be empty', 'error');
    expect(API.put).not.toHaveBeenCalled();
    expect(getUndoStack()).toEqual([]);
    const btn = document.getElementById('btn-pipeline-undo') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('re-pushes the entry and surfaces an error when the undo PUT fails', async () => {
    const { showToast } = await import('../../src/utils');
    document.body.innerHTML = `<button id="btn-pipeline-undo" disabled></button>`;
    const btn = document.getElementById('btn-pipeline-undo') as HTMLButtonElement;
    vi.mocked(API.put).mockRejectedValue(new Error('server down'));

    pushUndoEntry('span-1', 'prior text');
    expect(btn.disabled).toBe(false);

    await undoLastSpanEdit();

    // The entry is re-pushed so a later retry can still revert it.
    expect(getUndoStack()).toEqual([{ spanId: 'span-1', priorValue: 'prior text' }]);
    expect(btn.disabled).toBe(false);
    expect(showToast).toHaveBeenCalledWith('Failed to undo span text edit', 'error');
  });

  it('index.html declares the Undo button in the editor toolbar, disabled initially', () => {
    const html = readIndexHtml();

    expect(html).toContain('id="btn-pipeline-undo"');
    expect(html).toMatch(/id="btn-pipeline-undo"[^>]*disabled/);
    expect(html).toContain('>Undo</button>');
    const editorTabStart = html.indexOf('id="editor-tab"');
    const undoIdx = html.indexOf('id="btn-pipeline-undo"');
    expect(editorTabStart).toBeGreaterThan(-1);
    expect(undoIdx).toBeGreaterThan(editorTabStart);
  });
});
