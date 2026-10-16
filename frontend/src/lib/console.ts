/**
 * Deep links into the AWS Management Console for THIS deployment's resources (names come from
 * config.json, written from the stack outputs). They only work for someone signed in to the console of
 * the same account; the app itself never needs console access.
 *
 * Console URLs are not a stable public API: if AWS moves a page, the link lands on the service's home
 * page and the resource is one search away.
 */
import type { AppConfig } from '../config';

function regional(region: string, path: string): string {
  return `https://${region}.console.aws.amazon.com/${path}`;
}

/** Stack ids are `ExpenseFlow-{Stage}-{Name}` (SPEC §1); Lambda names start with the stack id. */
function stackPrefix(stage: string): string {
  return `ExpenseFlow-${stage.charAt(0).toUpperCase()}${stage.slice(1)}`;
}

export function consoleLinks(config: AppConfig) {
  const r = encodeURIComponent(config.region);
  const c = config.console;
  const at = (path: string) => regional(config.region, path);
  return {
    table: at(`dynamodbv2/home?region=${r}#table?name=${encodeURIComponent(c.tableName)}`),
    tableItems: at(`dynamodbv2/home?region=${r}#item-explorer?table=${encodeURIComponent(c.tableName)}`),
    receiptsBucket: `https://s3.console.aws.amazon.com/s3/buckets/${encodeURIComponent(c.receiptsBucket)}?region=${r}&tab=objects`,
    reportsBucket: `https://s3.console.aws.amazon.com/s3/buckets/${encodeURIComponent(c.reportsBucket)}?region=${r}&tab=objects`,
    eventBus: at(`events/home?region=${r}#/eventbus/${encodeURIComponent(c.eventBusName)}`),
    eventRules: at(`events/home?region=${r}#/rules?eventbus=${encodeURIComponent(c.eventBusName)}`),
    approvalWorkflow: stateMachine(config, c.approvalStateMachineArn),
    receiptPipeline: stateMachine(config, c.receiptStateMachineArn),
    dashboard: at(`cloudwatch/home?region=${r}#dashboards/dashboard/${encodeURIComponent(c.dashboardName)}`),
    alarms: at(`cloudwatch/home?region=${r}#alarmsV2:`),
    logsInsights: at(`cloudwatch/home?region=${r}#logsV2:logs-insights`),
    traceMap: at(`cloudwatch/home?region=${r}#xray:service-map/map`),
    httpApi: at(`apigateway/main/api-detail?api=${encodeURIComponent(c.httpApiId)}&region=${r}`),
    webSocketApi: at(`apigateway/main/api-detail?api=${encodeURIComponent(c.webSocketApiId)}&region=${r}`),
    userPool: at(`cognito/v2/idp/user-pools/${encodeURIComponent(c.userPoolId)}/users?region=${r}`),
    lambdaFunctions: at(`lambda/home?region=${r}#/functions?fo=and&o0=%3A&v0=${encodeURIComponent(stackPrefix(config.stage))}`),
    // CloudFront is a global service; its console lives in us-east-1 whatever the stack region is.
    distribution: `https://us-east-1.console.aws.amazon.com/cloudfront/v4/home?region=us-east-1#/distributions/${encodeURIComponent(c.distributionId)}`,
  };
}

function stateMachine(config: AppConfig, arn: string): string {
  const r = encodeURIComponent(config.region);
  return regional(config.region, `states/home?region=${r}#/statemachines/view/${encodeURIComponent(arn)}`);
}

/** One Step Functions execution (the ARN stored on the expense as `workflowExecutionArn`). */
export function executionLink(config: AppConfig, executionArn: string): string {
  const r = encodeURIComponent(config.region);
  return regional(config.region, `states/home?region=${r}#/v2/executions/details/${encodeURIComponent(executionArn)}`);
}

/** One object in a bucket (e.g. the receipt `receipts/{owner}/{expense}/{id}.jpg`). */
export function s3ObjectLink(config: AppConfig, bucket: string, key: string): string {
  return `https://s3.console.aws.amazon.com/s3/object/${encodeURIComponent(bucket)}?region=${encodeURIComponent(config.region)}&prefix=${encodeURIComponent(key)}`;
}

/**
 * CloudWatch Logs Insights query that follows one request: API Gateway writes `requestId` into its
 * access log, and Powertools' API resolver uses the same id as `correlation_id` in the Lambda's logs.
 */
export function logsInsightsQuery(requestId: string): string {
  return [
    'fields @timestamp, @log, @message',
    `| filter @message like "${requestId.replace(/"/g, '')}"`,
    '| sort @timestamp asc',
    '| limit 50',
  ].join('\n');
}
