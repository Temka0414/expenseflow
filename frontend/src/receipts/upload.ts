import type { Api } from '../api/client';
import { requestLog } from '../api/inspector';
import type { ObjectUploader } from '../platform/platform';
import type { PreparedReceipt } from './prepare';

export interface UploadDeps {
  api: Api;
  uploadObject: ObjectUploader;
}

/**
 * Two-step upload: (1) ask the API for a presigned POST (it checks ownership/status and records the
 * pending key), (2) send the bytes straight to S3. S3 then emits "Object Created" to EventBridge,
 * which starts the ReceiptPipeline state machine - the browser is done at that point.
 */
export async function uploadReceipt(
  deps: UploadDeps,
  expenseId: string,
  receipt: PreparedReceipt,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  const presigned = await deps.api.requestReceiptUpload(expenseId, {
    contentType: receipt.contentType,
    size: receipt.blob.size,
  });

  const startedAt = new Date().toISOString();
  const started = performance.now();
  const host = safeHost(presigned.url);
  try {
    const result = await deps.uploadObject(presigned, receipt.blob, onProgress, signal);
    requestLog.record({
      target: 's3',
      method: 'POST',
      path: `${host} (${presigned.key})`,
      status: result.status,
      durationMs: Math.round(performance.now() - started),
      requestId: result.requestId,
      startedAt,
      error: null,
    });
  } catch (error) {
    requestLog.record({
      target: 's3',
      method: 'POST',
      path: `${host} (${presigned.key})`,
      status: 0,
      durationMs: Math.round(performance.now() - started),
      requestId: null,
      startedAt,
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
  return presigned.key;
}

function safeHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
