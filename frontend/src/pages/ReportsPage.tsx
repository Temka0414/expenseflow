import { useState, type FormEvent } from 'react';
import { toApiError, type ApiError } from '../api/errors';
import type { ReportInfo } from '../api/types';
import { toasts } from '../components/toastStore';
import { Card, EmptyState, ErrorNotice, LoadingBlock, Notice, PageHeader, Spinner } from '../components/ui';
import { DETAIL_TYPES } from '../live/events';
import { useLiveRefresh } from '../live/LiveContext';
import { currentMonth, isValidIsoMonth, previousMonth } from '../lib/dates';
import { formatBytes, formatDateTime, formatMonth } from '../lib/format';
import { useResource } from '../lib/useResource';
import { useApi } from '../platform/context';

/** Save a file from a URL: a presigned S3 GET (AWS) or a data: URL (demo). */
function download(url: string, filename: string): void {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

export default function ReportsPage() {
  const api = useApi();
  const reports = useResource('reports', () => api.listReports());
  const [month, setMonth] = useState(previousMonth);
  const [generating, setGenerating] = useState<string | null>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);

  useLiveRefresh(
    (event) => event.detailType === DETAIL_TYPES.reportGenerated,
    () => {
      setGenerating(null);
      reports.reload();
    },
  );

  const monthProblem = !isValidIsoMonth(month)
    ? 'Choose a month.'
    : month > currentMonth()
      ? 'Reports can only cover past or current months.'
      : null;

  async function generate(event: FormEvent) {
    event.preventDefault();
    if (monthProblem) return;
    setFailure(null);
    try {
      const accepted = await api.generateReport(month);
      setGenerating(accepted.month);
      toasts.push({
        title: `Generating the ${formatMonth(accepted.month)} report`,
        text: 'The API invoked report_generator asynchronously (202 Accepted). You will be notified when the CSV is ready.',
      });
    } catch (error) {
      setFailure(toApiError(error));
    }
  }

  async function fetchReport(report: ReportInfo) {
    setDownloading(report.month);
    setFailure(null);
    try {
      const { url } = await api.getReportUrl(report.month);
      download(url, `expenseflow-${report.month}.csv`);
    } catch (error) {
      setFailure(toApiError(error));
    } finally {
      setDownloading(null);
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Monthly reports"
        description="CSV files of approved expenses (UTF-8 with BOM, so Excel opens Japanese text correctly). EventBridge Scheduler generates last month's report at 09:00 JST on the 1st."
      />
      {failure && <ErrorNotice error={failure} />}
      <div className="grid-2">
        <Card title="Generate a report" description="Runs report_generator now: it queries GSI2 for the month's approved expenses.">
          <form className="stack" onSubmit={(event) => void generate(event)} noValidate>
            <div className="field">
              <label className="field__label" htmlFor="report-month">
                Month
              </label>
              <input
                id="report-month"
                className="input"
                type="month"
                value={month}
                max={currentMonth()}
                onChange={(event) => setMonth(event.target.value)}
                aria-invalid={monthProblem ? true : undefined}
                aria-describedby="report-month-hint"
              />
              <span className={monthProblem ? 'field__error' : 'field__hint'} id="report-month-hint">
                {monthProblem ?? 'Defaults to the previous month, like the scheduled run.'}
              </span>
            </div>
            <div className="form-actions">
              <button type="submit" className="btn btn-primary" disabled={monthProblem !== null || generating !== null}>
                {generating ? <Spinner small /> : null} Generate CSV
              </button>
            </div>
            {generating && (
              <Notice title={`Generating ${formatMonth(generating)}…`}>
                The list refreshes when the &ldquo;Report Generated&rdquo; event arrives over the WebSocket.
              </Notice>
            )}
          </form>
        </Card>
        <Card title="Reports" description="Stored in the private reports bucket; downloads use presigned URLs." flush>
          {reports.error && <ErrorNotice error={reports.error} onRetry={reports.reload} />}
          {reports.loading && <LoadingBlock />}
          {reports.data && reports.data.length === 0 && <EmptyState title="No reports yet">Generate one on the left.</EmptyState>}
          {reports.data && reports.data.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Month</th>
                    <th className="right">Size</th>
                    <th>Generated</th>
                    <th>
                      <span className="sr-only">Download</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {reports.data.map((report) => (
                    <tr key={report.key}>
                      <td>{formatMonth(report.month)}</td>
                      <td className="right num">{formatBytes(report.size)}</td>
                      <td>{formatDateTime(report.lastModified)}</td>
                      <td className="right">
                        <button type="button" className="btn btn-sm" onClick={() => void fetchReport(report)} disabled={downloading !== null}>
                          {downloading === report.month && <Spinner small />} Download
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
