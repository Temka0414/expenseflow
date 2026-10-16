import { isReceiptContentType, RECEIPT_CONTENT_TYPES, type ReceiptContentType } from '../domain';
import { formatBytes } from '../lib/format';
import { encodeToFit, MAX_IMAGE_EDGE, needsDownscale, type Size } from './image';

export const ACCEPT_ATTRIBUTE = RECEIPT_CONTENT_TYPES.join(',');

const EXTENSION_TYPES: Record<string, ReceiptContentType> = {
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  png: 'image/png',
  webp: 'image/webp',
  gif: 'image/gif',
  pdf: 'application/pdf',
};

/** The browser's MIME type, or a guess from the extension when the OS did not provide one. */
export function receiptContentType(file: Pick<File, 'type' | 'name'>): ReceiptContentType | null {
  if (isReceiptContentType(file.type)) return file.type;
  if (file.type !== '') return null;
  const extension = file.name.split('.').pop()?.toLowerCase() ?? '';
  return EXTENSION_TYPES[extension] ?? null;
}

export interface PreparedReceipt {
  blob: Blob;
  contentType: ReceiptContentType;
  /** Set when the image was downscaled, for a "4032x3024 (5.1 MB) -> 2000x1500 (610 KB)" note. */
  resized: { from: Size; to: Size; fromBytes: number } | null;
}

/** Validate the type and size, downscaling images when they are too big in bytes or pixels. */
export async function prepareReceipt(file: File, maxBytes: number): Promise<PreparedReceipt> {
  const contentType = receiptContentType(file);
  if (!contentType) {
    throw new Error(`"${file.name}" is not a supported receipt. Use a JPEG, PNG, WebP or GIF image, or a PDF.`);
  }
  if (contentType === 'application/pdf') {
    if (file.size > maxBytes) {
      throw new Error(`This PDF is ${formatBytes(file.size)}; the limit is ${formatBytes(maxBytes)}.`);
    }
    return { blob: file, contentType, resized: null };
  }

  let bitmap: ImageBitmap;
  try {
    // createImageBitmap honours EXIF orientation by default, so portrait phone photos stay upright.
    bitmap = await createImageBitmap(file);
  } catch {
    throw new Error(`Could not read "${file.name}" as an image.`);
  }
  try {
    const original = { width: bitmap.width, height: bitmap.height };
    if (!needsDownscale(original, file.size, maxBytes, MAX_IMAGE_EDGE)) return { blob: file, contentType, resized: null };
    const encoded = await encodeToFit(bitmap, maxBytes, MAX_IMAGE_EDGE);
    return {
      blob: encoded.blob,
      contentType: 'image/jpeg',
      resized: { from: original, to: encoded.size, fromBytes: file.size },
    };
  } finally {
    bitmap.close();
  }
}
