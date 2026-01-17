/**
 * Category inference utilities for Polymarket markets
 */

// ============================================================================
// Category Patterns
// ============================================================================

const CATEGORY_PATTERNS: Array<{ pattern: RegExp; category: string }> = [
  { pattern: /bitcoin|btc|ethereum|eth|solana|crypto|token|blockchain|defi|nft/i, category: 'Crypto' },
  { pattern: /stock|nasdaq|s&p|dow|market|trading|price|above|below|nvda|aapl|tsla|msft|amzn|googl/i, category: 'Finance' },
  { pattern: /trump|biden|election|president|congress|senate|vote|political|democrat|republican|governor/i, category: 'Politics' },
  { pattern: /nfl|nba|mlb|nhl|soccer|football|basketball|baseball|hockey|game|win|score|playoffs|super bowl|championship/i, category: 'Sports' },
  { pattern: /ai|artificial intelligence|openai|chatgpt|google|apple|microsoft|tech|software|hardware|launch/i, category: 'Tech' },
  { pattern: /movie|film|tv|show|netflix|disney|spotify|youtube|streaming|box office|celebrity/i, category: 'Entertainment' },
  { pattern: /weather|temperature|hurricane|tornado|snow|rain|climate/i, category: 'Weather' },
];

// ============================================================================
// Functions
// ============================================================================

export function inferCategory(question: string): string | null {
  for (const { pattern, category } of CATEGORY_PATTERNS) {
    if (pattern.test(question)) {
      return category;
    }
  }
  return null;
}

export function formatCategory(category: string): string {
  return category
    .split(/[-_]/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ')
    .trim();
}

// ============================================================================
// Tag Extraction Types
// ============================================================================

export interface TagSource {
  category?: string;
  tags?: { label: string }[];
  events?: Array<{
    category?: string;
    tags?: { label: string }[];
  }>;
}

export function extractTags(source: TagSource, question: string): string[] {
  const categories: string[] = [];

  if (source.category && typeof source.category === 'string') {
    categories.push(formatCategory(source.category));
  }

  if (source.events && Array.isArray(source.events)) {
    for (const event of source.events) {
      if (event.category && typeof event.category === 'string') {
        const formatted = formatCategory(event.category);
        if (!categories.includes(formatted)) {
          categories.push(formatted);
        }
      }
      if (event.tags && Array.isArray(event.tags)) {
        for (const tag of event.tags) {
          if (tag.label && tag.label !== 'All' && !categories.includes(tag.label)) {
            categories.push(tag.label);
          }
        }
      }
    }
  }

  if (source.tags && Array.isArray(source.tags)) {
    for (const tag of source.tags) {
      if (tag.label && tag.label !== 'All' && !categories.includes(tag.label)) {
        categories.push(tag.label);
      }
    }
  }

  if (categories.length === 0) {
    const inferredCategory = inferCategory(question);
    if (inferredCategory) {
      categories.push(inferredCategory);
    }
  }

  return categories.length > 0 ? categories : ['Uncategorized'];
}
