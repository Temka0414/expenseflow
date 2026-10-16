import type { ObjectUploader } from '../platform/platform';

/**
 * Browser -> S3 upload with a presigned POST policy.
 *
 * - The API signed a policy that pins the key, the exact Content-Type and a content-length-range;
 *   S3 itself enforces it, so no Lambda ever touches the file bytes.
 * - multipart/form-data: every policy field first, the file LAST - S3 ignores fields after the file.
 * - XMLHttpRequest instead of fetch because only XHR reports upload progress.
 * - S3 answers 204 No Content on success; errors are XML (`<Code>EntityTooLarge</Code>`, or AccessDenied
 *   "Invalid according to Policy" when the file does not match the signed conditions).
 */
export const uploadWithPresignedPost: ObjectUploader = (presigned, file, onProgress, signal) =>
  new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [name, value] of Object.entries(presigned.fields)) form.append(name, value);
    form.append('file', file);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', presigned.url);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      const requestId = xhr.getResponseHeader('x-amz-request-id');
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve({ status: xhr.status, requestId });
      } else {
        reject(new Error(describeS3Error(xhr.status, xhr.responseText)));
      }
    };
    xhr.onerror = () =>
      reject(new Error('The upload to S3 failed (network error or the bucket CORS rules rejected this origin).'));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    signal?.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(form);
  });

function describeS3Error(status: number, xml: string): string {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  const code = doc.querySelector('Code')?.textContent ?? `HTTP ${status}`;
  const message = doc.querySelector('Message')?.textContent;
  if (code === 'EntityTooLarge') return 'S3 rejected the file: it is larger than the signed policy allows.';
  if (code === 'AccessDenied' && message?.includes('Policy expired')) {
    return 'The upload URL expired (presigned POSTs are valid for 5 minutes). Please try again.';
  }
  return `S3 rejected the upload: ${code}${message ? ` - ${message}` : ''}`;
}
