import { useEffect, useRef } from 'react';
import * as d3 from 'd3';
import { CalibrationBucket } from '../types';

interface Props {
  data: CalibrationBucket[];
}

export default function CalibrationChart({ data }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    if (!svgRef.current || data.length === 0) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const margin = { top: 40, right: 80, bottom: 60, left: 60 };
    const width = 700 - margin.left - margin.right;
    const height = 400 - margin.top - margin.bottom;

    const g = svg
      .append('g')
      .attr('transform', `translate(${margin.left},${margin.top})`);

    // Scales
    const x = d3
      .scaleLinear()
      .domain([0, 1])
      .range([0, width]);

    const y = d3
      .scaleLinear()
      .domain([0, 1])
      .range([height, 0]);

    // Perfect calibration diagonal
    g.append('line')
      .attr('x1', x(0))
      .attr('y1', y(0))
      .attr('x2', x(1))
      .attr('y2', y(1))
      .attr('stroke', '#666')
      .attr('stroke-dasharray', '5,5')
      .attr('stroke-width', 2);

    // Axes
    g.append('g')
      .attr('transform', `translate(0,${height})`)
      .call(d3.axisBottom(x).tickFormat(d => `${(d as number) * 100}%`))
      .append('text')
      .attr('x', width / 2)
      .attr('y', 45)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .text('Implied No Probability (Price)');

    g.append('g')
      .call(d3.axisLeft(y).tickFormat(d => `${(d as number) * 100}%`))
      .append('text')
      .attr('transform', 'rotate(-90)')
      .attr('y', -45)
      .attr('x', -height / 2)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .text('Actual No Win Rate');

    // Confidence interval area
    const areaData = data.filter(d => d.marketCount > 0);

    const area = d3.area<CalibrationBucket>()
      .x(d => x((d.bucketMin + d.bucketMax) / 2))
      .y0(d => y(d.confidenceInterval95.lower))
      .y1(d => y(d.confidenceInterval95.upper))
      .curve(d3.curveMonotoneX);

    g.append('path')
      .datum(areaData)
      .attr('fill', 'rgba(66, 153, 225, 0.2)')
      .attr('d', area);

    // Actual win rate line
    const line = d3.line<CalibrationBucket>()
      .x(d => x((d.bucketMin + d.bucketMax) / 2))
      .y(d => y(d.actualNoWinRate))
      .curve(d3.curveMonotoneX);

    g.append('path')
      .datum(areaData)
      .attr('fill', 'none')
      .attr('stroke', '#4299e1')
      .attr('stroke-width', 3)
      .attr('d', line);

    // Data points
    g.selectAll('.point')
      .data(areaData)
      .enter()
      .append('circle')
      .attr('class', 'point')
      .attr('cx', d => x((d.bucketMin + d.bucketMax) / 2))
      .attr('cy', d => y(d.actualNoWinRate))
      .attr('r', d => Math.min(Math.sqrt(d.marketCount) + 3, 15))
      .attr('fill', d => d.isStatisticallySignificant ? '#48bb78' : '#4299e1')
      .attr('stroke', '#fff')
      .attr('stroke-width', 2)
      .style('cursor', 'pointer')
      .on('mouseover', function(event, d) {
        const tooltip = d3.select('#calibration-tooltip');
        tooltip
          .style('opacity', 1)
          .html(`
            <strong>${d.bucket}</strong><br/>
            Markets: ${d.marketCount}<br/>
            Actual: ${(d.actualNoWinRate * 100).toFixed(1)}%<br/>
            Expected: ${(d.expectedNoWinRate * 100).toFixed(1)}%<br/>
            Edge: ${(d.edge * 100).toFixed(1)}%<br/>
            ${d.isStatisticallySignificant ? '<span style="color:#48bb78">Significant</span>' : 'Not significant'}
          `)
          .style('left', `${event.pageX + 10}px`)
          .style('top', `${event.pageY - 10}px`);
      })
      .on('mouseout', function() {
        d3.select('#calibration-tooltip').style('opacity', 0);
      });

    // Legend
    const legend = g.append('g')
      .attr('transform', `translate(${width - 120}, 20)`);

    const legendItems = [
      { y: 0, stroke: '#666', dasharray: '5,5', width: 2, label: 'Perfect' },
      { y: 20, stroke: '#4299e1', dasharray: null, width: 3, label: 'Actual' },
    ];

    legendItems.forEach(item => {
      const line = legend.append('line')
        .attr('x1', 0).attr('y1', item.y).attr('x2', 30).attr('y2', item.y)
        .attr('stroke', item.stroke).attr('stroke-width', item.width);
      if (item.dasharray) line.attr('stroke-dasharray', item.dasharray);

      legend.append('text')
        .attr('x', 35).attr('y', item.y + 4).attr('fill', '#aaa').text(item.label);
    });

    // Title
    svg.append('text')
      .attr('x', (width + margin.left + margin.right) / 2)
      .attr('y', 20)
      .attr('fill', '#fff')
      .style('text-anchor', 'middle')
      .style('font-size', '16px')
      .style('font-weight', 'bold')
      .text('Market Calibration: Implied vs Actual No Win Rate');

  }, [data]);

  return (
    <div className="calibration-chart">
      <svg ref={svgRef} width={700} height={400} />
      <div id="calibration-tooltip" className="calibration-tooltip" />
      <style>{`
        .calibration-tooltip {
          position: absolute;
          opacity: 0;
          background: rgba(0,0,0,0.85);
          color: #fff;
          padding: 10px;
          border-radius: 4px;
          font-size: 12px;
          pointer-events: none;
          z-index: 1000;
        }
      `}</style>
    </div>
  );
}
