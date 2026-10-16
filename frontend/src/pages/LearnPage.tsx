import { useEffect, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router';
import { requestLog, useRequestLog, type InspectedRequest } from '../api/inspector';
import { toasts } from '../components/toastStore';
import { Card, EmptyState, PageHeader } from '../components/ui';
import { useLiveHub, useLiveSnapshot } from '../live/LiveContext';
import { consoleLinks, logsInsightsQuery } from '../lib/console';
import { formatDateTime, formatDuration, formatMoney, formatTime } from '../lib/format';
import { usePlatform } from '../platform/context';

interface Flow {
  title: string;
  steps: ReactNode[];
}

function flows(limit: string): Flow[] {
  return [
    {
      title: 'Create or edit an expense',
      steps: [
        <><span className="service">API Gateway HTTP API</span> checks the Cognito ID token with its JWT authorizer (no Lambda needed for that).</>,
        <><span className="service">Lambda</span> (one Powertools &ldquo;Lambdalith&rdquo;) validates the body with pydantic: bad input is a 422.</>,
        <><span className="service">DynamoDB</span> stores the item; edits use a condition on <code>version</code>, so a stale write gets 409 instead of silently overwriting.</>,
      ],
    },
    {
      title: 'Upload a receipt',
      steps: [
        <>The API returns a <span className="service">presigned S3 POST</span>: the browser uploads directly to S3, the bytes never pass through Lambda.</>,
        <><span className="service">S3</span> sends &ldquo;Object Created&rdquo; to the default <span className="service">EventBridge</span> bus; a rule starts the ReceiptPipeline.</>,
        <><span className="service">Step Functions</span> marks the expense PROCESSING and invokes the extract_receipt Lambda.</>,
        <><span className="service">Amazon Bedrock</span> runs Claude, which returns the fields through a tool call; the result is saved on the item.</>,
        <>The <span className="service">DynamoDB stream</span> triggers stream_processor, which publishes &ldquo;Receipt Processed&rdquo;; you see the suggestions live.</>,
      ],
    },
    {
      title: 'Submit for approval',
      steps: [
        <>A conditional write moves DRAFT → PENDING_APPROVAL (it fails if someone else changed the status first).</>,
        <>stream_processor turns the change into &ldquo;Expense Submitted&rdquo; on the custom bus: a transactional outbox, built from the stream.</>,
        <>An EventBridge rule starts the <span className="service">ApprovalWorkflow</span>; it claims the expense and reads the limit from <span className="service">SSM Parameter Store</span>.</>,
        <>At most {limit} with a verified receipt: auto-approved. Otherwise the workflow pauses with a <strong>task token</strong> until a manager decides.</>,
      ],
    },
    {
      title: 'Approve or reject',
      steps: [
        <>The manager&apos;s decision calls <code>SendTaskSuccess</code> with the stored token; the paused execution resumes.</>,
        <>RecordDecision writes the result; the stream publishes &ldquo;Expense Approved/Rejected&rdquo; and updates the monthly totals in one transaction.</>,
        <>Every event also lands in an <span className="service">SQS</span> queue for the notifier (email via <span className="service">SES</span>, when configured).</>,
      ],
    },
    {
      title: 'Monthly report',
      steps: [
        <><span className="service">EventBridge Scheduler</span> runs report_generator at 09:00 JST on the 1st (or an admin starts it).</>,
        <>It queries GSI2 for the month&apos;s approved expenses and writes a CSV to the reports bucket.</>,
        <>&ldquo;Report Generated&rdquo; is pushed to admins; downloads use short-lived presigned URLs.</>,
      ],
    },
    {
      title: 'Live updates',
      steps: [
        <>The browser opens the <span className="service">API Gateway WebSocket API</span> with the Cognito access token; a Lambda authorizer checks it.</>,
        <>Connections are stored in DynamoDB per audience (you, managers, admins) with a TTL.</>,
        <>realtime_push sends every domain event to the right connections; API Gateway closes connections after 2 hours, so the app reconnects with backoff.</>,
      ],
    },
  ];
}

function ResourceTable() {
  const { config, mode } = usePlatform();
  const links = consoleLinks(config);
  const c = config.console;
  const rows: Array<[label: string, value: string, href: string]> = [
    ['DynamoDB table', c.tableName, links.tableItems],
    ['Receipts bucket (S3)', c.receiptsBucket, links.receiptsBucket],
    ['Reports bucket (S3)', c.reportsBucket, links.reportsBucket],
    ['Event bus (EventBridge)', c.eventBusName, links.eventRules],
    ['ApprovalWorkflow (Step Functions)', c.approvalStateMachineArn.split(':').pop() ?? '', links.approvalWorkflow],
    ['ReceiptPipeline (Step Functions)', c.receiptStateMachineArn.split(':').pop() ?? '', links.receiptPipeline],
    ['HTTP API (API Gateway)', c.httpApiId, links.httpApi],
    ['WebSocket API (API Gateway)', c.webSocketApiId, links.webSocketApi],
    ['User pool (Cognito)', c.userPoolId, links.userPool],
    ['Lambda functions', `ExpenseFlow-${config.stage}…`, links.lambdaFunctions],
    ['Dashboard (CloudWatch)', c.dashboardName, links.dashboard],
    ['Traces (X-Ray)', 'Trace map', links.traceMap],
    ['CloudFront distribution', c.distributionId, links.distribution],
  ];
  return (
    <Card
      title="This deployment"
      description={
        mode === 'demo'
          ? 'Demo mode shows example names; a real deployment lists its own resources here (from config.json).'
          : `Stage ${config.stage} in ${config.region}. Links open the AWS console (sign in to the same account).`
      }
      flush
    >
      <div className="table-wrap">
        <table className="table">
          <tbody>
            {rows.map(([label, value, href]) => (
              <tr key={label}>
                <td>{label}</td>
                <td>
                  <code>{value}</code>
                </td>
                <td className="right">
                  <a href={href} target="_blank" rel="noreferrer">
                    Console ↗
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function LiveLog() {
  const hub = useLiveHub();
  const { status, connectedAt, log } = useLiveSnapshot();
  return (
    <Card
      title="Live event log"
      description={`Raw WebSocket messages as realtime_push sent them. Status: ${status}${connectedAt ? `, connected ${formatDateTime(connectedAt)}` : ''}.`}
      actions={
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => hub.clearLog()} disabled={log.length === 0}>
          Clear
        </button>
      }
      flush
    >
      <p className="subtle card__body">
        Endpoint: <code>{hub.source.description}</code>
      </p>
      {log.length === 0 ? (
        <EmptyState title="No events yet">Submit an expense or upload a receipt and watch the events arrive here.</EmptyState>
      ) : (
        <ul className="event-log">
          {log.map((entry) => (
            <li key={entry.seq}>
              <div className="row-between">
                <strong>{entry.event.detailType}</strong>
                <span className="subtle">
                  {entry.event.source} · {formatTime(entry.receivedAt)}
                </span>
              </div>
              <pre className="json">{JSON.stringify(entry.event, null, 2)}</pre>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function statusBadge(request: InspectedRequest): string {
  if (request.status === 0 || request.status >= 500) return 'badge--danger';
  if (request.status >= 400) return 'badge--warning';
  return 'badge--success';
}

function RequestInspector() {
  const { config } = usePlatform();
  const requests = useRequestLog();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = requests.find((request) => request.id === selectedId) ?? requests.find((request) => request.requestId !== null);
  const query = selected?.requestId ? logsInsightsQuery(selected.requestId) : null;

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toasts.push({ tone: 'success', title: 'Query copied' });
    } catch {
      toasts.push({ tone: 'warning', title: 'Copy failed', text: 'Select the query and copy it manually.' });
    }
  }

  return (
    <Card
      title="Request inspector"
      description="Every API call from this tab. Pick one to trace it through the API Gateway access log and the Lambda logs."
      actions={
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => requestLog.clear()} disabled={requests.length === 0}>
          Clear
        </button>
      }
      flush
    >
      {requests.length === 0 ? (
        <EmptyState title="No requests yet" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Request</th>
                <th>Status</th>
                <th className="right">Duration</th>
                <th>apigw-requestid</th>
              </tr>
            </thead>
            <tbody>
              {requests.map((request) => (
                <tr key={request.id} aria-selected={request.id === selected?.id}>
                  <td className="nowrap">{formatTime(request.startedAt)}</td>
                  <td>
                    <code>
                      {request.method} {request.target === 's3' ? `S3 ${request.path}` : request.path}
                    </code>
                    {request.error && <div className="field__error">{request.error}</div>}
                  </td>
                  <td>
                    <span className={`badge badge--plain ${statusBadge(request)}`}>{request.status || 'network'}</span>
                  </td>
                  <td className="right num nowrap">{formatDuration(request.durationMs)}</td>
                  <td>
                    {request.requestId ? (
                      <button type="button" className="link-button mono" onClick={() => setSelectedId(request.id)}>
                        {request.requestId}
                      </button>
                    ) : (
                      <span className="subtle">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {query && (
        <div className="card__body stack-sm">
          <h3>Find this request in CloudWatch Logs Insights</h3>
          <p className="muted">
            Select the HTTP API access log group and the ApiFunction log group, then run this query. API Gateway logs the{' '}
            <code>requestId</code>, and Powertools uses the same id as the Lambda log&apos;s correlation id.
          </p>
          <pre className="json">{query}</pre>
          <div className="row">
            <button type="button" className="btn btn-sm" onClick={() => void copy(query)}>
              Copy query
            </button>
            <a className="btn btn-sm btn-ghost" href={consoleLinks(config).logsInsights} target="_blank" rel="noreferrer">
              Open Logs Insights ↗
            </a>
          </div>
        </div>
      )}
    </Card>
  );
}

export default function LearnPage() {
  const { config } = usePlatform();
  const location = useLocation();

  useEffect(() => {
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView({ behavior: 'smooth' });
  }, [location.hash]);

  return (
    <div className="page">
      <PageHeader
        title="Under the hood"
        description="What each click does in AWS, the resources of this deployment, and the raw traffic of this browser tab."
      />
      <div className="grid-2">
        {flows(formatMoney(config.autoApproveLimit, config.defaultCurrency)).map((flow) => (
          <Card key={flow.title} title={flow.title}>
            <ol className="flow">
              {flow.steps.map((step, index) => (
                <li key={index}>
                  <span>{step}</span>
                </li>
              ))}
            </ol>
          </Card>
        ))}
      </div>
      <ResourceTable />
      <div id="live">
        <LiveLog />
      </div>
      <RequestInspector />
    </div>
  );
}
