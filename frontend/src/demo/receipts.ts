/**
 * Receipts the demo can "read". Real ExpenseFlow sends the image to Claude on Amazon Bedrock
 * (extract_receipt, SPEC §7.4); the demo has no model, so it knows the content of the receipts it
 * draws itself (exact results) and, for any other file, plausibly pretends to have read one of these.
 *
 * Japanese receipts usually show tax-inclusive prices (内税): the tax line says how much of the total
 * is consumption tax (10%, or 8% for take-away food), so subtotal + tax = total.
 */
import type { ExtractedLineItem, ExtractionResult } from '../api/types';
import type { Category } from '../domain';

export interface ReceiptTemplate {
  merchant: string;
  address: string;
  category: Category;
  paymentMethod: string;
  taxRate: number;
  items: Array<{ description: string; quantity: number; unitPrice: number }>;
}

export interface SampleReceipt {
  merchant: string;
  address: string;
  date: string;
  time: string;
  currency: 'JPY';
  category: Category;
  paymentMethod: string;
  taxRate: number;
  lineItems: ExtractedLineItem[];
  subtotal: number;
  tax: number;
  total: number;
}

export const RECEIPT_TEMPLATES = {
  coffee: {
    merchant: 'Hinode Coffee Shinjuku',
    address: '1-2-3 Nishi-Shinjuku, Shinjuku-ku, Tokyo',
    category: 'MEALS',
    paymentMethod: 'IC card (Suica)',
    taxRate: 0.08,
    items: [
      { description: 'Iced coffee (M)', quantity: 2, unitPrice: 380 },
      { description: 'Ham & cheese sandwich', quantity: 1, unitPrice: 520 },
      { description: 'Milk crepe', quantity: 1, unitPrice: 420 },
    ],
  },
  taxi: {
    merchant: 'Sakura Taxi Co., Ltd.',
    address: '4-5-6 Higashi-Shinagawa, Shinagawa-ku, Tokyo',
    category: 'TRANSPORT',
    paymentMethod: 'Credit card',
    taxRate: 0.1,
    items: [
      { description: 'Fare: Haneda Airport T3 to Shinagawa', quantity: 1, unitPrice: 6230 },
      { description: 'Expressway toll', quantity: 1, unitPrice: 620 },
    ],
  },
  hotel: {
    merchant: 'Hotel Aoba Sendai Station',
    address: '2-1-1 Chuo, Aoba-ku, Sendai, Miyagi',
    category: 'LODGING',
    paymentMethod: 'Credit card',
    taxRate: 0.1,
    items: [
      { description: 'Single room, 1 night', quantity: 1, unitPrice: 9600 },
      { description: 'Accommodation tax', quantity: 1, unitPrice: 200 },
    ],
  },
  shinkansen: {
    merchant: 'Shinkansen Ticket Office, Tokyo Station',
    address: '1-9-1 Marunouchi, Chiyoda-ku, Tokyo',
    category: 'TRAVEL',
    paymentMethod: 'Credit card',
    taxRate: 0.1,
    items: [{ description: 'Nozomi Tokyo - Shin-Osaka, reserved seat', quantity: 2, unitPrice: 14720 }],
  },
  books: {
    merchant: 'Mori Books Shinjuku',
    address: '3-17-7 Shinjuku, Shinjuku-ku, Tokyo',
    category: 'TRAINING',
    paymentMethod: 'Cash',
    taxRate: 0.1,
    items: [{ description: 'AWS Solutions Architect study guide', quantity: 1, unitPrice: 3520 }],
  },
  electronics: {
    merchant: 'Denki Plaza Yurakucho',
    address: '1-11-1 Yurakucho, Chiyoda-ku, Tokyo',
    category: 'SUPPLIES',
    paymentMethod: 'Credit card',
    taxRate: 0.1,
    items: [
      { description: 'USB-C hub, 7 ports', quantity: 1, unitPrice: 4980 },
      { description: 'HDMI cable 2 m', quantity: 1, unitPrice: 1280 },
    ],
  },
  ramen: {
    merchant: 'Ramen Tatsu Shibuya',
    address: '2-6-2 Dogenzaka, Shibuya-ku, Tokyo',
    category: 'MEALS',
    paymentMethod: 'Cash',
    taxRate: 0.1,
    items: [
      { description: 'Tonkotsu ramen', quantity: 2, unitPrice: 980 },
      { description: 'Extra noodles', quantity: 1, unitPrice: 210 },
      { description: 'Gyoza (6 pcs)', quantity: 1, unitPrice: 450 },
    ],
  },
} satisfies Record<string, ReceiptTemplate>;

export type TemplateId = keyof typeof RECEIPT_TEMPLATES;
const TEMPLATE_IDS = Object.keys(RECEIPT_TEMPLATES) as TemplateId[];

export function buildSample(templateId: TemplateId, date: string, time = '12:34'): SampleReceipt {
  const template: ReceiptTemplate = RECEIPT_TEMPLATES[templateId];
  const lineItems = template.items.map((item) => ({ ...item, amount: item.quantity * item.unitPrice }));
  const total = lineItems.reduce((sum, item) => sum + item.amount, 0);
  const tax = Math.floor((total * template.taxRate) / (1 + template.taxRate));
  return {
    merchant: template.merchant,
    address: template.address,
    date,
    time,
    currency: 'JPY',
    category: template.category,
    paymentMethod: template.paymentMethod,
    taxRate: template.taxRate,
    lineItems,
    subtotal: total - tax,
    tax,
    total,
  };
}

/** Deterministic pick for files the demo did not draw (same file -> same "reading"). */
export function templateForSeed(seed: number): TemplateId {
  return TEMPLATE_IDS[Math.abs(Math.trunc(seed)) % TEMPLATE_IDS.length];
}

export function randomTemplate(random: () => number): TemplateId {
  return TEMPLATE_IDS[Math.floor(random() * TEMPLATE_IDS.length)];
}

/** The `record_receipt` tool output extract_receipt would store (SPEC §7.4), for a known receipt. */
export function simulatedExtraction(
  sample: SampleReceipt,
  options: { model: string; confidence: ExtractionResult['confidence'] },
): ExtractionResult {
  return {
    isReceipt: true,
    merchant: sample.merchant,
    date: sample.date,
    currency: sample.currency,
    total: sample.total,
    subtotal: sample.subtotal,
    tax: sample.tax,
    paymentMethod: sample.paymentMethod,
    category: sample.category,
    confidence: options.confidence,
    lineItems: sample.lineItems,
    model: options.model,
    requestedModel: options.model,
    fallbackUsed: false,
    // Typical magnitudes for one receipt image with Claude (image tokens dominate the input).
    inputTokens: 1480 + sample.lineItems.length * 24,
    outputTokens: 260 + sample.lineItems.length * 38,
  };
}
