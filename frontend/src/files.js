// Shared client-side rules for bill uploads and chat history.

export const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
export const BILL_ACCEPT = 'application/pdf,image/png,image/jpeg';
export const BULK_ACCEPT = '.xlsx,.xlsm,.csv';

const BILL_TYPES = new Set(['application/pdf', 'image/png', 'image/jpeg']);
const BULK_EXT = /\.(xlsx|xlsm|csv)$/i;

export function fileError(file, kind = 'bill') {
  if (!file) return 'Choose a file first.';
  if (file.size > MAX_UPLOAD_BYTES) return 'That file is over 15 MB. Try a smaller file.';
  if (kind === 'bulk') {
    return BULK_EXT.test(file.name) ? null : 'Choose an .xlsx, .xlsm or .csv file.';
  }
  return BILL_TYPES.has(file.type) ? null : 'Choose a PDF, JPG or PNG file.';
}
