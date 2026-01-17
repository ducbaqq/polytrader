import { AlphaSummary, AlphaMarket, LiquidityTierStats, DurationStats } from '../types';
import CalibrationChart from './CalibrationChart';
import EdgeByCategoryChart from './EdgeByCategoryChart';
import EdgeScatterPlot from './EdgeScatterPlot';

interface Props {
  summary: AlphaSummary;
  markets: AlphaMarket[];
}

function formatPct(val: number): string {
  return `${(val * 100).toFixed(1)}%`;
}

function formatPctSigned(val: number): string {
  const sign = val >= 0 ? '+' : '';
  return `${sign}${(val * 100).toFixed(1)}%`;
}

function getEdgeColor(edge: number): string {
  return edge >= 0 ? '#48bb78' : '#f56565';
}

export default function EdgeAnalysisView({ summary, markets }: Props): JSX.Element {
  const { overall, byCalibrationBucket, byCategory, byLiquidity, byDuration, recommendations } =
    summary;

  return (
    <div className="edge-analysis-view">
      <OverallStats overall={overall} />

      {recommendations.length > 0 && <Recommendations items={recommendations} />}

      <ChartSection
        title="Market Calibration"
        description="How well do market prices predict outcomes? Points above the diagonal indicate No bets were underpriced."
      >
        <CalibrationChart data={byCalibrationBucket} />
      </ChartSection>

      <ChartSection
        title="Edge by Category"
        description="Which categories show consistent edge? Green bars indicate profitable No betting opportunities."
      >
        <EdgeByCategoryChart data={byCategory} />
      </ChartSection>

      <ChartSection
        title="Individual Markets"
        description="Each dot is a market. Position shows final No price (x) and outcome (y). Size reflects volume. Click category to filter."
      >
        <EdgeScatterPlot markets={markets} />
      </ChartSection>

      <BreakdownTable
        title="Edge by Liquidity Tier"
        data={byLiquidity}
        labelKey="tier"
      />

      <BreakdownTable
        title="Edge by Market Duration"
        data={byDuration}
        labelKey="duration"
      />

      <style>{styles}</style>
    </div>
  );
}

// ============================================================================
// Sub-components
// ============================================================================

interface OverallStatsProps {
  overall: AlphaSummary['overall'];
}

function OverallStats({ overall }: OverallStatsProps): JSX.Element {
  return (
    <section className="overall-stats">
      <h2>Overall Edge Analysis</h2>
      <div className="stats-grid">
        <StatCard label="Total Markets" value={overall.totalMarkets.toLocaleString()} />
        <StatCard
          label="No Win Rate"
          value={formatPct(overall.noWinRate)}
          valueColor="#48bb78"
        />
        <StatCard label="Avg No Price at Close" value={formatPct(overall.avgNoPriceAtClose)} />
        <StatCard
          label="Average Edge"
          value={formatPctSigned(overall.averageEdge)}
          valueColor={getEdgeColor(overall.averageEdge)}
          subtext={`95% CI: [${formatPctSigned(overall.confidenceInterval95.lower)}, ${formatPctSigned(overall.confidenceInterval95.upper)}]`}
          highlight
        />
      </div>
    </section>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  valueColor?: string;
  subtext?: string;
  highlight?: boolean;
}

function StatCard({ label, value, valueColor, subtext, highlight }: StatCardProps): JSX.Element {
  return (
    <div className={`stat-card ${highlight ? 'highlight' : ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </div>
      {subtext && <div className="stat-ci">{subtext}</div>}
    </div>
  );
}

interface RecommendationsProps {
  items: string[];
}

function Recommendations({ items }: RecommendationsProps): JSX.Element {
  return (
    <section className="recommendations">
      <h2>Key Findings</h2>
      <ul>
        {items.map((rec, i) => (
          <li key={i}>{rec}</li>
        ))}
      </ul>
    </section>
  );
}

interface ChartSectionProps {
  title: string;
  description: string;
  children: React.ReactNode;
}

function ChartSection({ title, description, children }: ChartSectionProps): JSX.Element {
  return (
    <section className="chart-section">
      <h2>{title}</h2>
      <p className="chart-description">{description}</p>
      {children}
    </section>
  );
}

interface BreakdownTableProps {
  title: string;
  data: (LiquidityTierStats | DurationStats)[];
  labelKey: 'tier' | 'duration';
}

function BreakdownTable({ title, data, labelKey }: BreakdownTableProps): JSX.Element {
  return (
    <section className="breakdown-section">
      <h2>{title}</h2>
      <table className="breakdown-table">
        <thead>
          <tr>
            <th>{labelKey === 'tier' ? 'Tier' : 'Duration'}</th>
            <th>Markets</th>
            <th>No Win Rate</th>
            <th>Avg No Price</th>
            <th>Edge</th>
            <th>95% CI</th>
            <th>Significant</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => {
            const label = labelKey === 'tier' ? (row as LiquidityTierStats).tier : (row as DurationStats).duration;
            return (
              <tr key={i} className={row.isStatisticallySignificant ? 'significant' : ''}>
                <td>{label}</td>
                <td>{row.marketCount}</td>
                <td>{formatPct(row.noWinRate)}</td>
                <td>{formatPct(row.avgNoPrice)}</td>
                <td style={{ color: getEdgeColor(row.edge) }}>{formatPctSigned(row.edge)}</td>
                <td>
                  [{formatPctSigned(row.confidenceInterval95.lower)},{' '}
                  {formatPctSigned(row.confidenceInterval95.upper)}]
                </td>
                <td>{row.isStatisticallySignificant ? '\u2713' : '-'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

// ============================================================================
// Styles
// ============================================================================

const styles = `
  .edge-analysis-view {
    padding: 20px;
    max-width: 1000px;
    margin: 0 auto;
  }

  .overall-stats {
    margin-bottom: 40px;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 20px;
    margin-top: 20px;
  }

  .stat-card {
    background: rgba(255,255,255,0.05);
    padding: 20px;
    border-radius: 8px;
    text-align: center;
  }

  .stat-card.highlight {
    background: rgba(66, 153, 225, 0.1);
    border: 1px solid rgba(66, 153, 225, 0.3);
  }

  .stat-label {
    font-size: 12px;
    color: #aaa;
    text-transform: uppercase;
    margin-bottom: 8px;
  }

  .stat-value {
    font-size: 28px;
    font-weight: bold;
  }

  .stat-ci {
    font-size: 11px;
    color: #888;
    margin-top: 8px;
  }

  .recommendations {
    background: rgba(72, 187, 120, 0.1);
    border: 1px solid rgba(72, 187, 120, 0.3);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 40px;
  }

  .recommendations h2 {
    margin-top: 0;
    color: #48bb78;
  }

  .recommendations ul {
    margin: 0;
    padding-left: 20px;
  }

  .recommendations li {
    margin-bottom: 10px;
    line-height: 1.5;
  }

  .chart-section {
    margin-bottom: 50px;
  }

  .chart-section h2 {
    margin-bottom: 10px;
  }

  .chart-description {
    color: #aaa;
    font-size: 14px;
    margin-bottom: 20px;
  }

  .breakdown-section {
    margin-bottom: 40px;
  }

  .breakdown-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 15px;
  }

  .breakdown-table th,
  .breakdown-table td {
    padding: 12px;
    text-align: left;
    border-bottom: 1px solid #333;
  }

  .breakdown-table th {
    background: rgba(255,255,255,0.05);
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    color: #aaa;
  }

  .breakdown-table tr.significant {
    background: rgba(72, 187, 120, 0.1);
  }

  .breakdown-table tr:hover {
    background: rgba(255,255,255,0.03);
  }

  h2 {
    color: #fff;
    font-size: 20px;
    border-bottom: 1px solid #333;
    padding-bottom: 10px;
  }
`;
