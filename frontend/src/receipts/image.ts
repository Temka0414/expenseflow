/**
 * Client-side receipt downscaling.
 *
 * Phone photos are typically 4000x3000 px and 3-8 MB. The receipts bucket only accepts files up to
 * `maxReceiptBytes` (3.5 MB: Bedrock limits images to 5 MB *base64*, about 3.75 MB raw), and Claude
 * gains nothing from more than ~2000 px on the long edge. Shrinking in the browser keeps uploads fast on
 * mobile networks and costs nothing server-side. PDFs are uploaded as-is (they cannot be re-encoded here).
 */

export const MAX_IMAGE_EDGE = 2000;
export const JPEG_QUALITY = 0.85;

export interface Size {
  width: number;
  height: number;
}

export interface EncodeAttempt extends Size {
  quality: number;
}

/** Scale `size` down (never up) so its longest edge is at most `maxEdge`, keeping the aspect ratio. */
export function fitWithin(size: Size, maxEdge: number): Size {
  const longest = Math.max(size.width, size.height);
  if (longest <= maxEdge) return { width: size.width, height: size.height };
  const scale = maxEdge / longest;
  return {
    width: Math.max(1, Math.round(size.width * scale)),
    height: Math.max(1, Math.round(size.height * scale)),
  };
}

export function needsDownscale(size: Size, bytes: number, maxBytes: number, maxEdge = MAX_IMAGE_EDGE): boolean {
  return bytes > maxBytes || Math.max(size.width, size.height) > maxEdge;
}

/**
 * Encoding attempts in order of preference: first the target size at good quality, then progressively
 * cheaper settings. Re-encoding stops at the first result that fits the byte limit.
 */
export function encodeAttempts(size: Size, maxEdge = MAX_IMAGE_EDGE): EncodeAttempt[] {
  const plan: Array<[edgeFactor: number, quality: number]> = [
    [1, JPEG_QUALITY],
    [1, 0.72],
    [0.8, 0.72],
    [0.6, 0.7],
  ];
  return plan.map(([edgeFactor, quality]) => ({ ...fitWithin(size, Math.round(maxEdge * edgeFactor)), quality }));
}

export interface EncodedImage {
  blob: Blob;
  size: Size;
}

/** Resize and re-encode a decoded image as JPEG until it fits `maxBytes` (browser only). */
export async function encodeToFit(bitmap: ImageBitmap, maxBytes: number, maxEdge = MAX_IMAGE_EDGE): Promise<EncodedImage> {
  let smallest = Infinity;
  for (const attempt of encodeAttempts({ width: bitmap.width, height: bitmap.height }, maxEdge)) {
    const blob = await renderJpeg(bitmap, attempt);
    if (blob.size <= maxBytes) return { blob, size: { width: attempt.width, height: attempt.height } };
    smallest = Math.min(smallest, blob.size);
  }
  throw new Error(`The image is still ${Math.round(smallest / 1024)} KB after compressing it. Try cropping the photo.`);
}

function renderJpeg(bitmap: ImageBitmap, attempt: EncodeAttempt): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = attempt.width;
  canvas.height = attempt.height;
  const context = canvas.getContext('2d');
  if (!context) return Promise.reject(new Error('Canvas 2D is not available in this browser.'));
  // JPEG has no alpha channel: paint white first or transparent PNG areas would turn black.
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, attempt.width, attempt.height);
  context.imageSmoothingQuality = 'high';
  context.drawImage(bitmap, 0, 0, attempt.width, attempt.height);
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('Could not encode the image.'))),
      'image/jpeg',
      attempt.quality,
    );
  });
}
