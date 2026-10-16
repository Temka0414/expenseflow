import type { Expense, ExtractionResult } from '../api/types';
import { suggestionsFor, type Suggestion } from '../expenses/suggestions';
import { formatDateTime, formatMoney, formatNumber } from '../lib/format';
import { usePlatform } from '../platform/context';
import { Notice, Spinner } from './ui';

const CONFIDENCE_CLASS: Record<ExtractionResult['confidence'], string> = {
  high: 'badge--success',
  medium: 'badge--warning',
  low: 'badge--danger',
};

function LineItems({ result, currency }: { result: ExtractionResult; currency: string }) {
  if (result.lineItems.length === 0) return null;
  const money = (value: number | null) => formatMoney(value, result.currency ?? currency);
  return (
    <details>
      <summary>
        {result.lineItems.length} line item{result.lineItems.length === 1 ? '' : 's'} read from the receipt
      </summary>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Item</th>
              <th className="right">Qty</th>
              <th className="right">Unit</th>
              <th className="right">Amount</th>
            </tr>
          </thead>
          <tbody>
            {result.lineItems.map((item, index) => (
              <tr key={index}>
                <td>{item.description ?? '—'}</td>
                <td className="right num">{item.quantity ?? '—'}</td>
                <td className="right num">{money(item.unitPrice)}</td>
                <td className="right num">{money(item.amount)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={3}>Subtotal / tax / total</td>
              <td className="right num">
                {money(result.subtotal)} / {money(result.tax)} / <strong>{money(result.total)}</strong>
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </details>
  );
}

function SuggestionList({
  suggestions,
  canApply,
  applying,
  onApply,
}: {
  suggestions: Suggestion[];
  canApply: boolean;
  applying: boolean;
  onApply: (suggestions: Suggestion[]) => void;
}) {
  const pending = suggestions.filter((suggestion) => !suggestion.matches);
  return (
    <div className="stack-sm">
      <ul className="suggestions">
        {suggestions.map((suggestion) => (
          <li key={suggestion.field} className={suggestion.matches ? 'suggestion suggestion--match' : 'suggestion'}>
            <span className="suggestion__label">{suggestion.label}</span>
            <span className="suggestion__value">
              {suggestion.display}
              {!suggestion.matches && <span className="suggestion__current">now: {suggestion.currentDisplay}</span>}
            </span>
            {suggestion.matches ? (
              <span className="badge badge--success">applied</span>
            ) : (
              canApply && (
                <button type="button" className="btn btn-sm" disabled={applying} onClick={() => onApply([suggestion])}>
                  Apply
                </button>
              )
            )}
          </li>
        ))}
      </ul>
      {canApply && pending.length > 1 && (
        <div>
          <button type="button" className="btn btn-primary btn-sm" disabled={applying} onClick={() => onApply(pending)}>
            Apply all {pending.length} suggestions
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * What Claude (via Amazon Bedrock) read from the receipt. The model only *suggests*: nothing changes
 * until the owner clicks Apply, which is a normal PATCH guarded by the expense version.
 */
export function AiPanel({
  expense,
  canApply,
  applying,
  onApply,
}: {
  expense: Expense;
  canApply: boolean;
  applying: boolean;
  onApply: (suggestions: Suggestion[]) => void;
}) {
  const { config } = usePlatform();
  const extraction = expense.extraction;

  if (!extraction) {
    if (expense.pendingUpload) {
      return (
        <p className="row muted">
          <Spinner small /> Receipt uploaded. Waiting for S3&apos;s &ldquo;Object Created&rdquo; event to start the ReceiptPipeline…
        </p>
      );
    }
    return (
      <p className="muted">
        {config.extractionEnabled
          ? 'Upload a receipt and Claude will read the merchant, date, total and category for you.'
          : 'AI extraction is switched off for this deployment (extractionModelId = none).'}
      </p>
    );
  }

  switch (extraction.status) {
    case 'PROCESSING':
      return (
        <p className="row muted">
          <Spinner small /> Claude is reading the receipt (Step Functions → Lambda → Amazon Bedrock). This takes a few
          seconds; the page updates by itself.
        </p>
      );
    case 'SKIPPED':
      return <p className="muted">Receipt stored. AI extraction is switched off for this deployment.</p>;
    case 'FAILED':
      return (
        <Notice tone="warning" title="Claude could not read this receipt">
          <span>{extraction.error ?? 'Unknown error.'}</span>
          <span className="subtle">Fill in the fields by hand, or upload a clearer photo.</span>
        </Notice>
      );
    case 'SUCCEEDED':
      break;
  }

  const result = extraction.result;
  if (!result) return <p className="muted">The extraction finished without a result.</p>;
  const suggestions = suggestionsFor(result, expense);

  return (
    <div className="stack">
      <div className="row">
        <span className={`badge ${CONFIDENCE_CLASS[result.confidence]}`}>{result.confidence} confidence</span>
        {result.fallbackUsed && <span className="badge badge--warning">fallback model used</span>}
      </div>
      {!result.isReceipt && (
        <Notice tone="warning" title="This does not look like a receipt">
          Claude flagged the file as not being a receipt. Double-check before applying anything.
        </Notice>
      )}
      {suggestions.length > 0 ? (
        <SuggestionList suggestions={suggestions} canApply={canApply} applying={applying} onApply={onApply} />
      ) : (
        <p className="muted">No usable fields were found on the receipt.</p>
      )}
      <LineItems result={result} currency={expense.currency} />
      <p className="subtle">
        Model <code>{result.model}</code> · {formatNumber(result.inputTokens)} input / {formatNumber(result.outputTokens)} output
        tokens · read {formatDateTime(extraction.extractedAt)}
      </p>
    </div>
  );
}
