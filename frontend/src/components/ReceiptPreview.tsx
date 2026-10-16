import type { ReceiptInfo } from '../api/types';
import { useResource } from '../lib/useResource';
import { useApi } from '../platform/context';
import { ErrorNotice, LoadingBlock } from './ui';

/**
 * Shows the stored receipt. The bucket is private: the API hands out a presigned GET URL valid for
 * 5 minutes (enough to load it once), so the browser fetches the bytes straight from S3.
 */
export function ReceiptPreview({ expenseId, receipt }: { expenseId: string; receipt: ReceiptInfo }) {
  const api = useApi();
  const link = useResource(`receipt:${receipt.key}`, () => api.getReceiptUrl(expenseId));

  if (link.error) return <ErrorNotice error={link.error} title="Could not load the receipt" onRetry={link.reload} />;
  if (!link.data) return <LoadingBlock label="Loading receipt…" />;
  const { url } = link.data;
  return (
    <div className="stack-sm">
      <div className="receipt-preview">
        {receipt.contentType === 'application/pdf' ? (
          <object data={url} type="application/pdf" aria-label="Receipt PDF">
            <a href={url} target="_blank" rel="noreferrer">
              Open the PDF receipt
            </a>
          </object>
        ) : (
          <img src={url} alt="Receipt" />
        )}
      </div>
      <a className="subtle" href={url} target="_blank" rel="noreferrer">
        Open in a new tab (presigned URL, expires in {Math.round(link.data.expiresIn / 60)} min)
      </a>
    </div>
  );
}
