import { useEffect, useRef } from 'react';
import * as d3 from 'd3';
import { CategoryStats } from '../types';

interface Props {
  data: CategoryStats[];
  maxCategories?: number;
}

export default function EdgeByCategoryChart({ data, maxCategories = 15 }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    if (!svgRef.current || data.length === 0) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    // Filter and sort by edge, take top N
    const filtered = data
      .filter(d => d.marketCount >= 10)
      .sort((a, b) => b.edge - a.edge)
      .slice(0, maxCategories);

    const margin = { top: 40, right: 150, bottom: 60, left: 150 };
    const width = 800 - margin.left - margin.right;
    const height = Math.max(400, filtered.length * 30);

    svg.attr('height', height + margin.top + margin.bottom);

    const g = svg
      .append('g')
      .attr('transform', `translate(${margin.left},${margin.top})`);

    // Scales
    const maxEdge = Math.max(Math.abs(d3.min(filtered, d => d.confidenceInterval95.lower) || 0),
                             Math.abs(d3.max(filtered, d => d.confidenceInterval95.upper) || 0), 0.15);

    const x = d3
      .scaleLinear()
      .domain([-maxEdge, maxEdge])
      .range([0, width]);

    const y = d3
      .scaleBand<string>()
      .domain(filtered.map(d => d.category))
      .range([0, height])
      .padding(0.2);

    // Zero line
    g.append('line')
      .attr('x1', x(0))
      .attr('y1', 0)
      .attr('x2', x(0))
      .attr('y2', height)
      .attr('stroke', '#666')
      .attr('stroke-width', 2);

    // Confidence interval bars with end caps
    const ciGroups = g.selectAll('.ci-group')
      .data(filtered)
      .enter()
      .append('g')
      .attr('class', 'ci-group');

    // Horizontal CI bar
    ciGroups.append('line')
      .attr('x1', d => x(d.confidenceInterval95.lower))
      .attr('y1', d => (y(d.category) || 0) + y.bandwidth() / 2)
      .attr('x2', d => x(d.confidenceInterval95.upper))
      .attr('y2', d => (y(d.category) || 0) + y.bandwidth() / 2)
      .attr('stroke', '#888')
      .attr('stroke-width', 2);

    // Left and right end caps
    ['lower', 'upper'].forEach(bound => {
      ciGroups.append('line')
        .attr('x1', d => x(d.confidenceInterval95[bound as 'lower' | 'upper']))
        .attr('y1', d => (y(d.category) || 0) + y.bandwidth() / 2 - 6)
        .attr('x2', d => x(d.confidenceInterval95[bound as 'lower' | 'upper']))
        .attr('y2', d => (y(d.category) || 0) + y.bandwidth() / 2 + 6)
        .attr('stroke', '#888')
        .attr('stroke-width', 2);
    });

    // Edge bars
    g.selectAll('.edge-bar')
      .data(filtered)
      .enter()
      .append('rect')
      .attr('class', 'edge-bar')
      .attr('x', d => d.edge >= 0 ? x(0) : x(d.edge))
      .attr('y', d => (y(d.category) || 0) + y.bandwidth() / 4)
      .attr('width', d => Math.abs(x(d.edge) - x(0)))
      .attr('height', y.bandwidth() / 2)
      .attr('fill', d => d.edge >= 0 ? '#48bb78' : '#f56565')
      .attr('opacity', d => d.verdict === 'insufficient_data' ? 0.4 : 0.8);

    // Point estimates
    g.selectAll('.point')
      .data(filtered)
      .enter()
      .append('circle')
      .attr('class', 'point')
      .attr('cx', d => x(d.edge))
      .attr('cy', d => (y(d.category) || 0) + y.bandwidth() / 2)
      .attr('r', 6)
      .attr('fill', d => d.edge >= 0 ? '#48bb78' : '#f56565')
      .attr('stroke', '#fff')
      .attr('stroke-width', 2);

    // Y axis (category names)
    g.append('g')
      .call(d3.axisLeft(y))
      .selectAll('text')
      .style('font-size', '11px');

    // X axis
    g.append('g')
      .attr('transform', `translate(0,${height})`)
      .call(d3.axisBottom(x).tickFormat(d => `${((d as number) * 100).toFixed(0)}%`))
      .append('text')
      .attr('x', width / 2)
      .attr('y', 45)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .text('Edge on No Bets (Actual - Implied)');

    // Sample size annotations
    g.selectAll('.sample-size')
      .data(filtered)
      .enter()
      .append('text')
      .attr('class', 'sample-size')
      .attr('x', width + 10)
      .attr('y', d => (y(d.category) || 0) + y.bandwidth() / 2 + 4)
      .attr('fill', '#aaa')
      .style('font-size', '10px')
      .text(d => `n=${d.marketCount}`);

    // Title
    svg.append('text')
      .attr('x', (width + margin.left + margin.right) / 2)
      .attr('y', 20)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .style('font-size', '16px')
      .style('font-weight', 'bold')
      .text('Edge by Category (with 95% Confidence Intervals)');

    // Legend
    const legend = g.append('g')
      .attr('transform', `translate(${width - 100}, 0)`);

    const legendItems = [
      { y: 0, fill: '#48bb78', label: 'Positive Edge' },
      { y: 25, fill: '#f56565', label: 'Negative Edge' },
    ];

    legendItems.forEach(item => {
      legend.append('rect')
        .attr('x', 0).attr('y', item.y).attr('width', 15).attr('height', 15)
        .attr('fill', item.fill);
      legend.append('text')
        .attr('x', 20).attr('y', item.y + 12).attr('fill', '#aaa').style('font-size', '11px')
        .text(item.label);
    });

  }, [data, maxCategories]);

  return (
    <div className="edge-by-category-chart">
      <svg ref={svgRef} width={800} height={600} />
    </div>
  );
}
