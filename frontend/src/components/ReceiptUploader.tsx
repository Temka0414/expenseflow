import { useRef, useState, type DragEvent } from 'react';
import { toApiError } from '../api/errors';
import { formatBytes } from '../lib/format';
import { usePlatform } from '../platform/context';
import { ACCEPT_ATTRIBUTE, prepareReceipt, type PreparedReceipt } from '../receipts/prepare';
import { uploadReceipt } from '../receipts/upload';
import { todayIso } from '../lib/dates';
import { Notice, ProgressBar, Spinner } from './ui';

type Phase = { name: 'idle' } | { name: 'preparing' } | { name: 'uploading'; progress: number } | { name: 'error'; message: string };

function resizeNote(prepared: PreparedReceipt): string | null {
  const resized = prepared.resized;
  if (!resized) return null;
  return `Downscaled in your browser from ${resized.from.width}×${resized.from.height} (${formatBytes(resized.fromBytes)}) to ${resized.to.width}×${resized.to.height} (${formatBytes(prepared.blob.size)}) before uploading.`;
}

/**
 * Drag & drop / file picker for a receipt. Big photos are downscaled first (canvas, JPEG), then the
 * file goes straight to S3 with a presigned POST; the API never sees the bytes.
 */
export function ReceiptUploader({
  expenseId,
  expenseDate,
  replacing,
  onUploadingChange,
  onUploaded,
}: {
  expenseId: string;
  expenseDate: string | null;
  replacing: boolean;
  onUploadingChange?: (uploading: boolean) => void;
  onUploaded: (note: string | null) => void;
}) {
  const platform = usePlatform();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>({ name: 'idle' });
  const [dragging, setDragging] = useState(false);
  const busy = phase.name === 'preparing' || phase.name === 'uploading';
  const { sampleReceipt } = platform;

  async function upload(file: File) {
    onUploadingChange?.(true);
    setPhase({ name: 'preparing' });
    try {
      const prepared = await prepareReceipt(file, platform.config.maxReceiptBytes);
      setPhase({ name: 'uploading', progress: 0 });
      await uploadReceipt(platform, expenseId, prepared, (progress) => setPhase({ name: 'uploading', progress }));
      setPhase({ name: 'idle' });
      onUploaded(resizeNote(prepared));
    } catch (error) {
      setPhase({ name: 'error', message: toApiError(error).message });
    } finally {
      onUploadingChange?.(false);
    }
  }

  async function uploadSample(sampleReceipt: (date: string) => Promise<File>) {
    await upload(await sampleReceipt(expenseDate ?? todayIso()));
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files.item(0);
    if (file && !busy) void upload(file);
  }

  return (
    <div className="stack-sm">
      <div
        className={dragging ? 'dropzone dropzone--active' : 'dropzone'}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {phase.name === 'preparing' && (
          <span className="row">
            <Spinner small /> Preparing the image…
          </span>
        )}
        {phase.name === 'uploading' && (
          <div className="stack-sm full-width">
            <span>Uploading to Amazon S3… {Math.round(phase.progress * 100)}%</span>
            <ProgressBar value={phase.progress} label="Receipt upload progress" />
          </div>
        )}
        {!busy && (
          <>
            <span className="dropzone__title">{replacing ? 'Replace the receipt' : 'Drop a receipt here'}</span>
            <span className="subtle">
              JPEG, PNG, WebP, GIF or PDF up to {formatBytes(platform.config.maxReceiptBytes)}. Large photos are resized
              automatically.
            </span>
            <div className="row row--center">
              <button type="button" className="btn btn-sm" onClick={() => inputRef.current?.click()}>
                Choose a file
              </button>
              {sampleReceipt && (
                <button type="button" className="btn btn-sm" onClick={() => void uploadSample(sampleReceipt)}>
                  Use a sample receipt
                </button>
              )}
            </div>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT_ATTRIBUTE}
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(event) => {
            const file = event.target.files?.item(0);
            event.target.value = '';
            if (file) void upload(file);
          }}
        />
      </div>
      {phase.name === 'error' && (
        <Notice tone="danger" title="Upload failed">
          {phase.message}
        </Notice>
      )}
    </div>
  );
}
