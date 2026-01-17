import { useState, useEffect } from 'react';
import { ExportData, CategoryData, AlphaSummary, AlphaAnalysisData } from './types';
import SummaryHeader from './components/SummaryHeader';
import BubbleChart from './components/BubbleChart';
import CategoryDetail from './components/CategoryDetail';
import EdgeAnalysisView from './components/EdgeAnalysisView';

type Tab = 'overview' | 'edge';

function App(): JSX.Element {
  const [data, setData] = useState<ExportData | null>(null);
  const [categories, setCategories] = useState<CategoryData[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<CategoryData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [alphaSummary, setAlphaSummary] = useState<AlphaSummary | null>(null);
  const [alphaMarkets, setAlphaMarkets] = useState<AlphaAnalysisData | null>(null);
  const [edgeLoading, setEdgeLoading] = useState(false);
  const [edgeError, setEdgeError] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<Tab>('overview');

  useEffect(() => {
    fetch('/settled_markets.json')
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load data');
        return res.json();
      })
      .then((exportData: ExportData) => {
        setData(exportData);
        setCategories(aggregateByCategory(exportData));
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    if (activeTab !== 'edge' || alphaSummary || edgeLoading || edgeError) {
      return;
    }

    setEdgeLoading(true);

    Promise.all([
      fetch('/alpha_summary.json').then((res) => {
        if (!res.ok) throw new Error('alpha_summary.json not found');
        return res.json();
      }),
      fetch('/alpha_analysis.json').then((res) => {
        if (!res.ok) throw new Error('alpha_analysis.json not found');
        return res.json();
      }),
    ])
      .then(([summary, analysis]) => {
        setAlphaSummary(summary);
        setAlphaMarkets(analysis);
        setEdgeLoading(false);
      })
      .catch((err) => {
        setEdgeError(err.message);
        setEdgeLoading(false);
      });
  }, [activeTab, alphaSummary, edgeLoading, edgeError]);

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

  const totalMarkets = data?.totalMarkets ?? 0;
  const totalYes = data?.markets.filter((m) => m.resolution === 'Yes').length ?? 0;
  const totalNo = totalMarkets - totalYes;
  const periodDisplay = data?.period ?? `${data?.monthsBack} months`;

  return (
    <div className="app">
      <header className="header">
        <h1>Polymarket Settled Markets</h1>
        <p className="subtitle">
          {periodDisplay} of data | Exported {data?.exportDate}
        </p>
      </header>

      <nav className="tab-nav">
        <button
          className={`tab-btn ${activeTab === 'overview' ? 'active' : ''}`}
          onClick={() => setActiveTab('overview')}
        >
          Market Overview
        </button>
        <button
          className={`tab-btn ${activeTab === 'edge' ? 'active' : ''}`}
          onClick={() => setActiveTab('edge')}
        >
          Edge Analysis
        </button>
      </nav>

      {activeTab === 'overview' && (
        <>
          <SummaryHeader totalMarkets={totalMarkets} totalYes={totalYes} totalNo={totalNo} />
          <div className="chart-container">
            <BubbleChart categories={categories} onCategoryClick={setSelectedCategory} />
          </div>
          {selectedCategory && (
            <CategoryDetail category={selectedCategory} onClose={() => setSelectedCategory(null)} />
          )}
        </>
      )}

      {activeTab === 'edge' && (
        <EdgeTabContent
          loading={edgeLoading}
          error={edgeError}
          summary={alphaSummary}
          markets={alphaMarkets}
        />
      )}

      <style>{`
        .tab-nav {
          display: flex;
          justify-content: center;
          gap: 10px;
          margin: 20px 0;
          padding: 0 20px;
        }

        .tab-btn {
          padding: 12px 24px;
          font-size: 14px;
          font-weight: 600;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.1);
          color: #aaa;
          border-radius: 8px;
          cursor: pointer;
          transition: all 0.2s;
        }

        .tab-btn:hover {
          background: rgba(255,255,255,0.1);
          color: #fff;
        }

        .tab-btn.active {
          background: rgba(66, 153, 225, 0.2);
          border-color: rgba(66, 153, 225, 0.5);
          color: #4299e1;
        }

        .error pre {
          background: rgba(0,0,0,0.3);
          padding: 10px 15px;
          border-radius: 4px;
          overflow-x: auto;
        }
      `}</style>
    </div>
  );
}

// ============================================================================
// Helper Components
// ============================================================================

interface EdgeTabContentProps {
  loading: boolean;
  error: string | null;
  summary: AlphaSummary | null;
  markets: AlphaAnalysisData | null;
}

function EdgeTabContent({ loading, error, summary, markets }: EdgeTabContentProps): JSX.Element {
  if (loading) {
    return <div className="loading">Loading edge analysis data...</div>;
  }

  if (error) {
    return (
      <div className="error">
        <h2>Edge Analysis Data Not Available</h2>
        <p>{error}</p>
        <p>Run the alpha analysis pipeline first:</p>
        <pre>npm run alpha-analysis -- --period 5d</pre>
        <p>Then copy the output files to the public folder:</p>
        <pre>cp alpha_summary.json alpha_analysis.json visualization/public/</pre>
      </div>
    );
  }

  if (summary && markets) {
    return <EdgeAnalysisView summary={summary} markets={markets.markets} />;
  }

  return <div className="loading">No data available</div>;
}

// ============================================================================
// Helper Functions
// ============================================================================

function aggregateByCategory(exportData: ExportData): CategoryData[] {
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

  return Array.from(categoryMap.values()).sort((a, b) => b.total - a.total);
}

export default App;
