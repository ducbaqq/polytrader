import { useEffect } from 'react';
import { CategoryData } from '../types';

interface CategoryDetailProps {
  category: CategoryData;
  onClose: () => void;
}

function CategoryDetail({ category, onClose }: CategoryDetailProps) {
  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  // Sort markets by date (most recent first)
  const sortedMarkets = [...category.markets].sort((a, b) => {
    return new Date(b.resolvedAt).getTime() - new Date(a.resolvedAt).getTime();
  });

  const yesPercent = ((category.yes / category.total) * 100).toFixed(1);
  const noPercent = ((category.no / category.total) * 100).toFixed(1);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2>{category.name}</h2>
            <div className="modal-stats">
              <span>{category.total.toLocaleString()} markets</span>
              <span className="yes">{category.yes.toLocaleString()} Yes ({yesPercent}%)</span>
              <span className="no">{category.no.toLocaleString()} No ({noPercent}%)</span>
            </div>
          </div>
          <button className="close-button" onClick={onClose}>
            &times;
          </button>
        </div>

        <div className="modal-content">
          <div className="market-list">
            {sortedMarkets.map((market) => (
              <div key={market.id} className="market-item">
                <div className="market-question">{market.question}</div>
                <div className="market-meta">
                  <span className={`resolution-badge ${market.resolution.toLowerCase()}`}>
                    {market.resolution}
                  </span>
                  <span className="market-date">{market.resolvedAt}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default CategoryDetail;
