/**
 * Draws a sample receipt as a PNG on a canvas, so demo mode can show the whole receipt flow
 * (upload -> "Claude reads it" -> suggestions -> auto-approval) without the user owning a receipt.
 */
import { formatMoney } from '../lib/format';
import type { SampleReceipt } from './receipts';

const WIDTH = 560;
const MARGIN = 36;
const LINE = 24;
const INK = '#1f2328';
const FADED = '#6b717c';

/** Long item names are wrapped to the receipt width. */
function wrap(context: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const lines: string[] = [];
  let current = '';
  for (const word of text.split(' ')) {
    const candidate = current ? `${current} ${word}` : word;
    if (context.measureText(candidate).width > maxWidth && current) {
      lines.push(current);
      current = word;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  return lines;
}

export function drawReceipt(sample: SampleReceipt): Promise<Blob> {
  const height = 330 + sample.lineItems.length * (LINE * 2 + 6) + 180;
  const canvas = document.createElement('canvas');
  canvas.width = WIDTH;
  canvas.height = height;
  const context = canvas.getContext('2d');
  if (!context) return Promise.reject(new Error('Canvas 2D is not available in this browser.'));

  const money = (amount: number | null) => formatMoney(amount, sample.currency);
  const right = WIDTH - MARGIN;
  let y = MARGIN + 18;

  const text = (value: string, x: number, options: { size?: number; bold?: boolean; color?: string; align?: CanvasTextAlign } = {}) => {
    context.font = `${options.bold ? '700' : '400'} ${options.size ?? 17}px ui-monospace, Menlo, Consolas, monospace`;
    context.fillStyle = options.color ?? INK;
    context.textAlign = options.align ?? 'left';
    context.fillText(value, x, y);
  };
  const separator = () => {
    context.strokeStyle = '#9aa0a8';
    context.setLineDash([6, 5]);
    context.beginPath();
    context.moveTo(MARGIN, y - 8);
    context.lineTo(right, y - 8);
    context.stroke();
    context.setLineDash([]);
    y += LINE * 0.6;
  };

  context.fillStyle = '#fffdf7';
  context.fillRect(0, 0, WIDTH, height);

  text(sample.merchant, WIDTH / 2, { size: 23, bold: true, align: 'center' });
  y += LINE + 4;
  text(sample.address, WIDTH / 2, { size: 13, color: FADED, align: 'center' });
  y += LINE;
  text('TEL 03-0000-0000  REG #2  STAFF 07', WIDTH / 2, { size: 13, color: FADED, align: 'center' });
  y += LINE * 1.6;
  text('RECEIPT', WIDTH / 2, { size: 20, bold: true, align: 'center' });
  y += LINE * 1.4;
  text(`${sample.date.replace(/-/g, '/')}  ${sample.time}`, MARGIN, { size: 15 });
  text('No. 000417', right, { size: 15, align: 'right' });
  y += LINE;
  separator();

  for (const item of sample.lineItems) {
    context.font = '400 17px ui-monospace, Menlo, Consolas, monospace';
    for (const line of wrap(context, item.description ?? '', WIDTH - MARGIN * 2 - 120)) {
      text(line, MARGIN);
      y += LINE;
    }
    text(`  ${item.quantity ?? 1} x ${money(item.unitPrice)}`, MARGIN, { size: 14, color: FADED });
    text(money(item.amount), right, { align: 'right' });
    y += LINE + 6;
  }
  separator();

  const taxLabel = `(incl. ${Math.round(sample.taxRate * 100)}% tax)`;
  text('Subtotal', MARGIN);
  text(money(sample.subtotal), right, { align: 'right' });
  y += LINE;
  text(`Tax ${taxLabel}`, MARGIN, { color: FADED, size: 15 });
  text(money(sample.tax), right, { align: 'right', color: FADED, size: 15 });
  y += LINE * 1.5;
  text('TOTAL', MARGIN, { size: 24, bold: true });
  text(money(sample.total), right, { size: 24, bold: true, align: 'right' });
  y += LINE * 1.4;
  text(`Paid by ${sample.paymentMethod}`, MARGIN, { size: 15 });
  y += LINE * 1.8;
  separator();
  text('Thank you for your visit!', WIDTH / 2, { size: 15, align: 'center' });
  y += LINE * 1.4;
  text('Sample receipt drawn by ExpenseFlow demo mode', WIDTH / 2, { size: 12, color: FADED, align: 'center' });

  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('Could not draw the sample receipt.'))), 'image/png');
  });
}
