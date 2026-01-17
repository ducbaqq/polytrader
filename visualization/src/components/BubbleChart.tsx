import { useEffect, useRef } from 'react';
import * as d3 from 'd3';
import { CategoryData } from '../types';

interface BubbleChartProps {
  categories: CategoryData[];
  onCategoryClick: (category: CategoryData) => void;
}

function BubbleChart({ categories, onCategoryClick }: BubbleChartProps) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    if (!svgRef.current || categories.length === 0) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const width = 900;
    const height = 600;

    svg.attr('viewBox', `0 0 ${width} ${height}`);

    // Create hierarchy data for pack layout
    const root = d3.hierarchy({
      children: categories.map(cat => ({
        ...cat,
        value: cat.total,
      })),
    })
      .sum(d => (d as any).value || 0)
      .sort((a, b) => (b.value || 0) - (a.value || 0));

    // Create pack layout
    const pack = d3.pack<typeof root.data>()
      .size([width - 20, height - 20])
      .padding(15);

    pack(root);

    // Create groups for each bubble
    const bubbles = svg.selectAll('.bubble-group')
      .data(root.leaves())
      .enter()
      .append('g')
      .attr('class', 'bubble-group')
      .attr('transform', d => `translate(${d.x + 10}, ${d.y + 10})`)
      .style('cursor', 'pointer')
      .on('click', (_, d) => {
        const catData = d.data as unknown as CategoryData;
        onCategoryClick(catData);
      });

    // Draw outer circle (background)
    bubbles.append('circle')
      .attr('class', 'bubble-circle')
      .attr('r', d => d.r)
      .attr('fill', '#1e293b')
      .attr('stroke', '#475569')
      .attr('stroke-width', 2);

    // Draw pie chart inside each bubble
    bubbles.each(function(d) {
      const catData = d.data as unknown as CategoryData;
      const group = d3.select(this);
      const radius = d.r * 0.85;

      if (catData.total === 0) return;

      const pieData = [
        { type: 'yes', value: catData.yes, color: '#22c55e' },
        { type: 'no', value: catData.no, color: '#ef4444' },
      ];

      const pie = d3.pie<typeof pieData[0]>()
        .value(item => item.value)
        .sort(null);

      const arc = d3.arc<d3.PieArcDatum<typeof pieData[0]>>()
        .innerRadius(radius * 0.4)  // Donut style
        .outerRadius(radius);

      group.selectAll('.pie-slice')
        .data(pie(pieData))
        .enter()
        .append('path')
        .attr('class', 'pie-slice')
        .attr('d', arc)
        .attr('fill', slice => slice.data.color)
        .attr('opacity', 0.85)
        .style('pointer-events', 'none');
    });

    // Add category name label
    bubbles.append('text')
      .attr('class', 'bubble-label')
      .attr('y', d => {
        // Position based on bubble size
        if (d.r > 60) return -5;
        return 0;
      })
      .attr('text-anchor', 'middle')
      .attr('fill', '#f1f5f9')
      .attr('font-size', d => Math.min(d.r / 4, 14))
      .attr('font-weight', '600')
      .style('pointer-events', 'none')
      .text(d => {
        const catData = d.data as unknown as CategoryData;
        // Truncate long names for smaller bubbles
        const maxLen = Math.floor(d.r / 5);
        return catData.name.length > maxLen
          ? catData.name.substring(0, maxLen) + '...'
          : catData.name;
      });

    // Add count label
    bubbles.append('text')
      .attr('class', 'bubble-count')
      .attr('y', d => {
        if (d.r > 60) return 12;
        return 14;
      })
      .attr('text-anchor', 'middle')
      .attr('fill', '#94a3b8')
      .attr('font-size', d => Math.min(d.r / 5, 11))
      .style('pointer-events', 'none')
      .text(d => {
        const catData = d.data as unknown as CategoryData;
        return `${catData.total.toLocaleString()} markets`;
      });

  }, [categories, onCategoryClick]);

  return (
    <svg ref={svgRef} style={{ width: '100%', maxWidth: '900px', height: 'auto' }} />
  );
}

export default BubbleChart;
