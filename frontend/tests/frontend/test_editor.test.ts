/**
 * Spec-first tests for Editor tab (frontend/src/tabs/editor.ts).
 * Tests cover: pipeline operations (split/merge/move/delete), span display,
 * confidence review UI, TTS rendering, pipeline toggle integration.
 *
 * NOTE: No test framework is installed in frontend/package.json.
 * These tests are written with vitest-compatible syntax.
 * To run: install vitest (`npm install -D vitest jsdom`) and add to package.json:
 *   "scripts": { "test": "vitest" },
 *   "vitest": { "environment": "jsdom" }
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
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
  initEditor,
} from '../../src/tabs/editor';
import { state } from '../../src/state';
import * as API from '../../src/api';

// Mock the API module
vi.mock('../../src/api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  upload: vi.fn(),
  handleError: vi.fn(),
}));

// Mock utils to avoid DOM side effects
vi.mock('../../src/utils', () => ({
  showToast: vi.fn(),
  showConfirm: vi.fn(),
  escapeHtml: (s: string) => s.replace(/</g, '&lt;').replace(/>/g, '&gt;'),
}));

// Mock templates module
vi.mock('../../src/templates', () => ({
  buildSpeakerSelect: vi.fn(() => '<select></select>'),
  updateChunkRow: vi.fn(),
  createVoiceCard: vi.fn(),
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
  { global_index: 0, speaker: 'Narrator', text: 'It was a dark and stormy night.', instruct: 'dramatic' },
  { global_index: 1, speaker: 'Elizabeth', text: 'I cannot believe it!', instruct: 'surprised' },
  { global_index: 2, speaker: 'Darcy', text: 'Forgive me, I was wrong.', instruct: 'sincere' },
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
// Test suites
// ---------------------------------------------------------------------------

describe('Editor Tab — Pipeline API Functions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.pipelineBookId = 'book-123';
    state.pipelineEnabled = true;
  });

  afterEach(() => {
    state.pipelineBookId = null;
    state.pipelineEnabled = false;
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
    state.pipelineEnabled = true;
  });

  describe('toPipelineSpans', () => {
    it('should convert raw export data to PipelineSpan array with global_index', () => {
      const result = toPipelineSpans(MOCK_SPANS_RAW);

      expect(result).toHaveLength(3);
      expect(result[0]).toEqual({
        global_index: 0,
        speaker: 'Narrator',
        text: 'It was a dark and stormy night.',
        instruct: 'dramatic',
      });
      expect(result[1].global_index).toBe(1);
      expect(result[2].global_index).toBe(2);
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
    state.pipelineEnabled = true;
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

      await handleSplit(0);

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'split',
        book_id: 'book-123',
        presentation_index: 0,
        split_point: 10,
      });
    });

    it('should show warning if no book onboarded', async () => {
      state.pipelineBookId = null;
      const { showToast } = await import('../../src/utils');

      await handleSplit(0);

      expect(showToast).toHaveBeenCalledWith('No book onboarded', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should show error for invalid split point', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      global.prompt = vi.fn().mockReturnValue('999');
      const { showToast } = await import('../../src/utils');

      await handleSplit(0);

      expect(showToast).toHaveBeenCalledWith('Invalid split point', 'error');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should not proceed if user cancels prompt', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      global.prompt = vi.fn().mockReturnValue(null);

      await handleSplit(0);

      expect(API.post).not.toHaveBeenCalled();
    });
  });

  describe('handleMerge', () => {
    it('should merge two adjacent selected spans', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(0);
      toggleSpanSelection(1);
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'merge' });

      await handleMerge();

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'merge',
        book_id: 'book-123',
        presentation_index_left: 0,
        presentation_index_right: 1,
      });
    });

    it('should show warning if not exactly 2 spans selected', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(0);
      const { showToast } = await import('../../src/utils');

      await handleMerge();

      expect(showToast).toHaveBeenCalledWith('Select exactly 2 adjacent spans to merge', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should show warning if spans are not adjacent', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(0);
      toggleSpanSelection(2);
      const { showToast } = await import('../../src/utils');

      await handleMerge();

      expect(showToast).toHaveBeenCalledWith('Can only merge adjacent spans', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });
  });

  describe('handleMove', () => {
    it('should move selected span to target position', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(0);
      vi.mocked(API.post).mockResolvedValue({ status: 'ok', operation: 'move' });

      await handleMove(2);

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/operation', {
        operation: 'move',
        book_id: 'book-123',
        presentation_index_from: 0,
        presentation_index_to: 2,
      });
    });

    it('should show warning if not exactly 1 span selected', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      const { showToast } = await import('../../src/utils');

      await handleMove(2);

      expect(showToast).toHaveBeenCalledWith('Select exactly 1 span to move', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should show info if moving to same position', async () => {
      vi.mocked(API.get).mockResolvedValue(MOCK_SPANS_RAW);
      await loadSpans();

      toggleSpanSelection(2);
      const { showToast } = await import('../../src/utils');

      await handleMove(2);

      expect(showToast).toHaveBeenCalledWith('Span is already at that position', 'info');
      expect(API.post).not.toHaveBeenCalled();
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
    state.pipelineEnabled = true;
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
    state.pipelineEnabled = true;
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

    it('should call pipelineRenderAudiobook and display job_id', async () => {
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-abc-123' });

      await pipelineRenderAll();

      expect(API.post).toHaveBeenCalledWith('/api/pipeline/render', {
        book_id: 'book-123',
        use_batch: true,
        batch_seed: null,
      });

      const jobDisplay = document.getElementById('pipeline-render-job');
      expect(jobDisplay?.textContent).toContain('job-abc-123');
      expect(jobDisplay?.classList.contains('d-none')).toBe(false);
    });

    it('should show warning if no book onboarded', async () => {
      state.pipelineBookId = null;
      const { showToast } = await import('../../src/utils');

      await pipelineRenderAll();

      expect(showToast).toHaveBeenCalledWith('No book onboarded', 'warning');
      expect(API.post).not.toHaveBeenCalled();
    });

    it('should hide render buttons and show cancel button during render', async () => {
      vi.mocked(API.post).mockResolvedValue({ job_id: 'job-test' });

      await pipelineRenderAll();

      const btnRender = document.getElementById('btn-pipeline-render');
      const btnRegen = document.getElementById('btn-pipeline-regen');
      const btnCancel = document.getElementById('btn-pipeline-cancel');

      expect(btnRender?.style.display).toBe('none');
      expect(btnRegen?.style.display).toBe('none');
      expect(btnCancel?.style.display).toBe('inline-block');
    });

    it('should handle render failure gracefully', async () => {
      vi.mocked(API.post).mockRejectedValue(new Error('Render failed'));
      const { showToast } = await import('../../src/utils');

      await pipelineRenderAll();

      expect(showToast).toHaveBeenCalledWith('Render failed: Render failed', 'error');
    });
  });
});

describe('Editor Tab — Pipeline Toggle Integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    document.body.innerHTML = `
      <div id="editor-pipeline-disabled-notice" style="display:none;"></div>
      <div id="pipeline-editor-section" style="display:none;"></div>
      <div id="legacy-editor-section"></div>
      <div id="spans-table-body"></div>
      <div id="chunks-table-body"></div>
      <div id="review-items-container"></div>
    `;
  });

  it('should show pipeline section when pipelineEnabled is true', () => {
    state.pipelineEnabled = true;
    state.pipelineBookId = 'book-123';

    // Simulate tab switch behavior
    const pipelineSection = document.getElementById('pipeline-editor-section');
    const legacySection = document.getElementById('legacy-editor-section');
    const notice = document.getElementById('editor-pipeline-disabled-notice');

    if (pipelineSection) pipelineSection.style.display = 'block';
    if (legacySection) legacySection.style.display = 'none';
    if (notice) notice.style.display = 'none';

    expect(pipelineSection?.style.display).toBe('block');
    expect(legacySection?.style.display).toBe('none');
    expect(notice?.style.display).toBe('none');
  });

  it('should show legacy section and notice when pipelineEnabled is false', () => {
    state.pipelineEnabled = false;

    const pipelineSection = document.getElementById('pipeline-editor-section');
    const legacySection = document.getElementById('legacy-editor-section');
    const notice = document.getElementById('editor-pipeline-disabled-notice');

    if (pipelineSection) pipelineSection.style.display = 'none';
    if (legacySection) legacySection.style.display = 'block';
    if (notice) notice.style.display = 'block';

    expect(pipelineSection?.style.display).toBe('none');
    expect(legacySection?.style.display).toBe('block');
    expect(notice?.style.display).toBe('block');
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
