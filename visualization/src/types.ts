export interface Market {
  id: string;
  question: string;
  tags: string[];
  resolution: 'Yes' | 'No';
  resolvedAt: string;
}

export interface ExportData {
  exportDate: string;
  monthsBack: number;
  totalMarkets: number;
  markets: Market[];
}

export interface CategoryData {
  name: string;
  total: number;
  yes: number;
  no: number;
  markets: Market[];
}
