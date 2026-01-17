interface SummaryHeaderProps {
  totalMarkets: number;
  totalYes: number;
  totalNo: number;
}

function SummaryHeader({ totalMarkets, totalYes, totalNo }: SummaryHeaderProps) {
  const yesPercent = totalMarkets > 0 ? ((totalYes / totalMarkets) * 100).toFixed(1) : '0';
  const noPercent = totalMarkets > 0 ? ((totalNo / totalMarkets) * 100).toFixed(1) : '0';

  return (
    <div className="summary-header">
      <div className="summary-stat">
        <div className="value">{totalMarkets.toLocaleString()}</div>
        <div className="label">Total Markets</div>
      </div>
      <div className="summary-stat yes">
        <div className="value">{totalYes.toLocaleString()}</div>
        <div className="label">Resolved Yes ({yesPercent}%)</div>
      </div>
      <div className="summary-stat no">
        <div className="value">{totalNo.toLocaleString()}</div>
        <div className="label">Resolved No ({noPercent}%)</div>
      </div>
    </div>
  );
}

export default SummaryHeader;
