/**
 * AWS-mode bootstrap: fetch /config.json, configure Amplify (Cognito), then render. Configuration is
 * loaded at runtime, not baked in at build time, so one build artifact serves every stage.
 */
import '@aws-amplify/ui-react/styles.css';
import { Amplify } from 'aws-amplify';
import { StrictMode } from 'react';
import type { Root } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { createApi } from '../api/client';
import { createFetchTransport } from '../api/fetchTransport';
import { BootError } from '../app/BootError';
import { loadConfig, type AppConfig } from '../config';
import { webSocketSource } from '../live/socket';
import type { Platform } from '../platform/platform';
import { uploadWithPresignedPost } from '../receipts/s3Upload';
import { accessToken, idToken } from './session';
import { AwsApp } from './AwsApp';

function configureAmplify(config: AppConfig): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.userPoolId,
        userPoolClientId: config.userPoolClientId,
        loginWith: { email: true },
        signUpVerificationMethod: 'code',
        userAttributes: { email: { required: true }, name: { required: true } },
      },
    },
  });
}

export async function startAws(root: Root): Promise<void> {
  let config: AppConfig;
  try {
    config = await loadConfig();
  } catch (error) {
    root.render(
      <StrictMode>
        <BootError error={error} />
      </StrictMode>,
    );
    return;
  }
  configureAmplify(config);

  const platform: Platform = {
    mode: 'aws',
    config,
    api: createApi(createFetchTransport(config.apiUrl, idToken)),
    live: webSocketSource(config.wsUrl, accessToken),
    uploadObject: uploadWithPresignedPost,
  };

  root.render(
    <StrictMode>
      <BrowserRouter>
        <AwsApp platform={platform} />
      </BrowserRouter>
    </StrictMode>,
  );
}
