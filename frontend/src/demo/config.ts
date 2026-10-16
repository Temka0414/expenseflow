/**
 * The config.json the demo pretends to have been deployed with (SPEC §8.5). Resource names follow the
 * shapes CloudFormation generates, so the Learn page shows what a real deployment looks like.
 */
import type { AppConfig } from '../config';

const REGION = 'ap-northeast-1';
const ACCOUNT = '123456789012';

/** Tokyo deployments call Claude through the Japan geo inference profile (SPEC §7.4). */
export const DEMO_MODEL = 'jp.anthropic.claude-opus-5-5';

export const DEMO_CONFIG: AppConfig = {
  region: REGION,
  stage: 'dev',
  userPoolId: `${REGION}_DeMo0Pool`,
  userPoolClientId: 'demo0client0id0000000000000',
  apiUrl: `https://demo0httpapi.execute-api.${REGION}.amazonaws.com`,
  wsUrl: `wss://demo0wsapi0.execute-api.${REGION}.amazonaws.com/live`,
  maxReceiptBytes: 3_500_000,
  defaultCurrency: 'JPY',
  autoApproveLimit: 5000,
  extractionEnabled: true,
  console: {
    tableName: 'ExpenseFlow-Dev-Data-TableCD117FA1-DEMO0000',
    receiptsBucket: 'expenseflow-dev-data-receiptsbucket-demo0000',
    reportsBucket: 'expenseflow-dev-data-reportsbucket-demo0000',
    eventBusName: 'ExpenseFlowDevBackendEventBusDEMO0000',
    approvalStateMachineArn: `arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:ApprovalWorkflowDEMO0000`,
    receiptStateMachineArn: `arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:ReceiptPipelineDEMO0000`,
    dashboardName: 'ExpenseFlow-dev',
    httpApiId: 'demo0httpapi',
    webSocketApiId: 'demo0wsapi0',
    userPoolId: `${REGION}_DeMo0Pool`,
    distributionId: 'E1DEMO0DISTRIB',
  },
};

export const DEMO_ACCOUNT = ACCOUNT;
