import { describe, expect, it } from 'vitest';
import { encodeAttempts, fitWithin, JPEG_QUALITY, MAX_IMAGE_EDGE, needsDownscale } from './image';
import { receiptContentType } from './prepare';

const MAX_BYTES = 3_500_000;

describe('fitWithin', () => {
  it('scales a landscape phone photo so the long edge is 2000 px', () => {
    expect(fitWithin({ width: 4032, height: 3024 }, MAX_IMAGE_EDGE)).toEqual({ width: 2000, height: 1500 });
  });

  it('scales a portrait photo by its height', () => {
    expect(fitWithin({ width: 3024, height: 4032 }, MAX_IMAGE_EDGE)).toEqual({ width: 1500, height: 2000 });
  });

  it('never upscales and keeps exact-limit images untouched', () => {
    expect(fitWithin({ width: 800, height: 600 }, MAX_IMAGE_EDGE)).toEqual({ width: 800, height: 600 });
    expect(fitWithin({ width: 2000, height: 10 }, MAX_IMAGE_EDGE)).toEqual({ width: 2000, height: 10 });
  });

  it('keeps at least one pixel for extreme aspect ratios', () => {
    expect(fitWithin({ width: 10_000, height: 2 }, MAX_IMAGE_EDGE)).toEqual({ width: 2000, height: 1 });
  });

  it('rounds to whole pixels', () => {
    expect(fitWithin({ width: 3000, height: 2001 }, MAX_IMAGE_EDGE)).toEqual({ width: 2000, height: 1334 });
  });
});

describe('needsDownscale', () => {
  it('is needed when the file is larger than the upload limit', () => {
    expect(needsDownscale({ width: 1200, height: 900 }, MAX_BYTES + 1, MAX_BYTES)).toBe(true);
  });

  it('is needed when the long edge exceeds 2000 px even if the file is small', () => {
    expect(needsDownscale({ width: 2001, height: 900 }, 400_000, MAX_BYTES)).toBe(true);
  });

  it('is not needed for a small, modest image (uploaded untouched, no quality loss)', () => {
    expect(needsDownscale({ width: 2000, height: 1500 }, MAX_BYTES, MAX_BYTES)).toBe(false);
  });
});

describe('encodeAttempts', () => {
  it('starts at the full target size and quality, then trades quality and size for bytes', () => {
    expect(encodeAttempts({ width: 4032, height: 3024 })).toEqual([
      { width: 2000, height: 1500, quality: JPEG_QUALITY },
      { width: 2000, height: 1500, quality: 0.72 },
      { width: 1600, height: 1200, quality: 0.72 },
      { width: 1200, height: 900, quality: 0.7 },
    ]);
  });

  it('only re-encodes small-pixel images at lower quality (they are big because of bytes, not pixels)', () => {
    const attempts = encodeAttempts({ width: 1000, height: 1000 });
    expect(attempts.map((a) => a.width)).toEqual([1000, 1000, 1000, 1000]);
  });
});

describe('receiptContentType', () => {
  it('trusts a supported browser MIME type', () => {
    expect(receiptContentType({ type: 'image/webp', name: 'x.bin' })).toBe('image/webp');
  });

  it('guesses from the extension when the OS gave no type', () => {
    expect(receiptContentType({ type: '', name: 'Receipt.JPG' })).toBe('image/jpeg');
    expect(receiptContentType({ type: '', name: 'invoice.pdf' })).toBe('application/pdf');
  });

  it('rejects unsupported types such as HEIC', () => {
    expect(receiptContentType({ type: 'image/heic', name: 'IMG_0001.HEIC' })).toBeNull();
    expect(receiptContentType({ type: '', name: 'notes.txt' })).toBeNull();
  });
});
