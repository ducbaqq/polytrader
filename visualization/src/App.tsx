import { useState, useEffect } from 'react';
import { ExportData, CategoryData, Market } from './types';
import SummaryHeader from './components/SummaryHeader';
import BubbleChart from './components/BubbleChart';
import CategoryDetail from './components/CategoryDetail';

function App() {
  const [data, setData] = useState<ExportData | null>(null);
  const [categories, setCategories] = useState<CategoryData[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<CategoryData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch('/settled_markets.json')
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load data');
        return res.json();
      })
      .then((exportData: ExportData) => {
        setData(exportData);

        // Aggregate by category
        const categoryMap = new Map<string, CategoryData>();

        for (const market of exportData.markets) {
          for (const tag of market.tags) {
            if (!categoryMap.has(tag)) {
              categoryMap.set(tag, {
                name: tag,
                total: 0,
                yes: 0,
                no: 0,
                markets: [],
              });
            }

            const cat = categoryMap.get(tag)!;
            cat.total++;
            if (market.resolution === 'Yes') {
              cat.yes++;
            } else {
              cat.no++;
            }
            cat.markets.push(market);
          }
        }

        // Convert to array and sort by total descending
        const categoryArray = Array.from(categoryMap.values())
          .sort((a, b) => b.total - a.total);

        setCategories(categoryArray);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  // Calculate totals (unique markets, not category duplicates)
  const totalMarkets = data?.totalMarkets ?? 0;
  const totalYes = data?.markets.filter(m => m.resolution === 'Yes').length ?? 0;
  const totalNo = data?.markets.filter(m => m.resolution === 'No').length ?? 0;

  if (loading) {
    return (
      <div className="app">
        <div className="loading">Loading market data...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="app">
        <div className="error">
          <h2>Error loading data</h2>
          <p>{error}</p>
          <p>Make sure settled_markets.json is in the public folder.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Polymarket Settled Markets</h1>
        <p className="subtitle">
          {data?.monthsBack} months of data | Exported {data?.exportDate}
        </p>
      </header>

      <SummaryHeader
        totalMarkets={totalMarkets}
        totalYes={totalYes}
        totalNo={totalNo}
      />

      <div className="chart-container">
        <BubbleChart
          categories={categories}
          onCategoryClick={setSelectedCategory}
        />
      </div>

      {selectedCategory && (
        <CategoryDetail
          category={selectedCategory}
          onClose={() => setSelectedCategory(null)}
        />
      )}
    </div>
  );
}

export default App;
