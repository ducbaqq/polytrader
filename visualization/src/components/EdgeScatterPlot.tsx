import { useEffect, useRef, useState } from 'react';
import * as d3 from 'd3';
import { AlphaMarket } from '../types';

interface Props {
  markets: AlphaMarket[];
}

export default function EdgeScatterPlot({ markets }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);

  // Get unique categories
  const categories = Array.from(new Set(markets.flatMap(m => m.tags))).slice(0, 10);

  useEffect(() => {
    if (!svgRef.current || markets.length === 0) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const margin = { top: 40, right: 150, bottom: 60, left: 60 };
    const width = 800 - margin.left - margin.right;
    const height = 500 - margin.top - margin.bottom;

    const g = svg
      .append('g')
      .attr('transform', `translate(${margin.left},${margin.top})`);

    // Filter by selected category
    const filteredMarkets = selectedCategory
      ? markets.filter(m => m.tags.includes(selectedCategory))
      : markets;

    // Color scale for categories
    const colorScale = d3.scaleOrdinal<string>()
      .domain(categories)
      .range(d3.schemeCategory10);

    // Scales
    const x = d3.scaleLinear()
      .domain([0, 1])
      .range([0, width]);

    const y = d3.scaleLinear()
      .domain([-0.2, 1.2])  // Allow for jitter
      .range([height, 0]);

    const volumeScale = d3.scaleSqrt()
      .domain([0, d3.max(filteredMarkets, d => d.volumeNum) || 10000])
      .range([3, 15]);

    // Jitter function for y-axis
    const jitter = () => (Math.random() - 0.5) * 0.15;

    // Perfect calibration line
    g.append('line')
      .attr('x1', x(0)).attr('y1', y(1))
      .attr('x2', x(1)).attr('y2', y(0))
      .attr('stroke', '#666')
      .attr('stroke-dasharray', '5,5')
      .attr('stroke-width', 2);

    // 50% reference lines (vertical and horizontal)
    const refLines = [
      { x1: x(0.5), y1: y(0), x2: x(0.5), y2: y(1) },
      { x1: x(0), y1: y(0.5), x2: x(1), y2: y(0.5) },
    ];
    refLines.forEach(coords => {
      g.append('line')
        .attr('x1', coords.x1).attr('y1', coords.y1)
        .attr('x2', coords.x2).attr('y2', coords.y2)
        .attr('stroke', '#444')
        .attr('stroke-dasharray', '3,3')
        .attr('stroke-width', 1);
    });

    // Data points
    g.selectAll('.market-point')
      .data(filteredMarkets)
      .enter()
      .append('circle')
      .attr('class', 'market-point')
      .attr('cx', d => x(d.prices.finalNoPrice))
      .attr('cy', d => y(d.edge.actualNoOutcome + jitter()))
      .attr('r', d => volumeScale(d.volumeNum))
      .attr('fill', d => colorScale(d.tags[0] || 'Unknown'))
      .attr('opacity', 0.6)
      .attr('stroke', '#fff')
      .attr('stroke-width', 0.5)
      .style('cursor', 'pointer')
      .on('mouseover', function(event, d) {
        d3.select(this).attr('opacity', 1).attr('stroke-width', 2);
        const tooltip = d3.select('#scatter-tooltip');
        tooltip
          .style('opacity', 1)
          .html(`
            <strong>${d.question.slice(0, 80)}${d.question.length > 80 ? '...' : ''}</strong><br/>
            <br/>
            Resolution: <strong style="color:${d.resolution === 'No' ? '#48bb78' : '#f56565'}">${d.resolution}</strong><br/>
            Final No Price: ${(d.prices.finalNoPrice * 100).toFixed(1)}%<br/>
            Edge: ${(d.edge.rawEdge * 100).toFixed(1)}%<br/>
            Volume: $${d.volumeNum.toLocaleString()}<br/>
            Category: ${d.tags.join(', ')}
          `)
          .style('left', `${event.pageX + 10}px`)
          .style('top', `${event.pageY - 10}px`);
      })
      .on('mouseout', function() {
        d3.select(this).attr('opacity', 0.6).attr('stroke-width', 0.5);
        d3.select('#scatter-tooltip').style('opacity', 0);
      });

    // Axes
    g.append('g')
      .attr('transform', `translate(0,${height})`)
      .call(d3.axisBottom(x).tickFormat(d => `${((d as number) * 100).toFixed(0)}%`))
      .append('text')
      .attr('x', width / 2)
      .attr('y', 45)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .text('Final No Price');

    g.append('g')
      .call(d3.axisLeft(y).tickValues([0, 1]).tickFormat(d => d === 0 ? 'Yes' : 'No'))
      .append('text')
      .attr('transform', 'rotate(-90)')
      .attr('y', -45)
      .attr('x', -height / 2)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .text('Resolution');

    // Legend
    const legend = g.append('g')
      .attr('transform', `translate(${width + 20}, 0)`);

    legend.append('text')
      .attr('x', 0)
      .attr('y', -5)
      .attr('fill', '#fff')
      .style('font-weight', 'bold')
      .style('font-size', '12px')
      .text('Categories');

    categories.forEach((cat, i) => {
      const row = legend.append('g')
        .attr('transform', `translate(0, ${i * 22 + 10})`)
        .style('cursor', 'pointer')
        .on('click', () => {
          setSelectedCategory(selectedCategory === cat ? null : cat);
        });

      row.append('circle')
        .attr('cx', 8)
        .attr('cy', 0)
        .attr('r', 6)
        .attr('fill', colorScale(cat))
        .attr('opacity', selectedCategory === null || selectedCategory === cat ? 1 : 0.3);

      row.append('text')
        .attr('x', 20)
        .attr('y', 4)
        .attr('fill', selectedCategory === null || selectedCategory === cat ? '#fff' : '#666')
        .style('font-size', '11px')
        .text(cat.length > 12 ? cat.slice(0, 12) + '...' : cat);
    });

    // Title
    svg.append('text')
      .attr('x', (width + margin.left + margin.right) / 2)
      .attr('y', 20)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .style('font-size', '16px')
      .style('font-weight', 'bold')
      .text('Individual Market Outcomes vs Final Price');

  }, [markets, selectedCategory, categories]);

  return (
    <div className="edge-scatter-plot">
      <svg ref={svgRef} width={800} height={500} />
      <div id="scatter-tooltip" className="scatter-tooltip" />
      {selectedCategory && (
        <button className="clear-filter-btn" onClick={() => setSelectedCategory(null)}>
          Clear Filter ({selectedCategory})
        </button>
      )}
      <style>{`
        .scatter-tooltip {
          position: absolute;
          opacity: 0;
          background: rgba(0,0,0,0.9);
          color: #fff;
          padding: 12px;
          border-radius: 4px;
          font-size: 12px;
          pointer-events: none;
          z-index: 1000;
          max-width: 350px;
        }
        .clear-filter-btn {
          margin-top: 10px;
          padding: 5px 15px;
          background: #444;
          color: #fff;
          border: none;
          border-radius: 4px;
          cursor: pointer;
        }
        .clear-filter-btn:hover {
          background: #555;
        }
      `}</style>
    </div>
  );
}
