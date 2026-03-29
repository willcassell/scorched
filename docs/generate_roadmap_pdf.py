#!/usr/bin/env python3
"""Generate the Scorched AI Trading Bot Improvement Roadmap PDF."""

import io
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import numpy as np
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, Color, white, black
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    PageBreak, KeepTogether, HRFlowable
)
from reportlab.platypus.flowables import Flowable
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ── Color Palette ──
DARK_BLUE = HexColor('#1B2A4A')
MID_BLUE = HexColor('#2E4A7A')
LIGHT_BLUE = HexColor('#4A90D9')
ACCENT_ORANGE = HexColor('#E8913A')
ACCENT_GOLD = HexColor('#D4A843')
SOFT_GRAY = HexColor('#F4F5F7')
MED_GRAY = HexColor('#9BA3AF')
TEXT_DARK = HexColor('#2C3E50')
TEXT_LIGHT = HexColor('#FFFFFF')
GREEN = HexColor('#27AE60')
RED = HexColor('#E74C3C')
TIER_GREEN = HexColor('#27AE60')
TIER_BLUE = HexColor('#3498DB')
TIER_ORANGE = HexColor('#E67E22')
TIER_RED = HexColor('#E74C3C')
TIER_GOLD = HexColor('#D4A843')
TIER_PURPLE = HexColor('#8E44AD')

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(OUTPUT_DIR, 'scorched-improvement-roadmap.pdf')

# ── Styles ──
styles = getSampleStyleSheet()

styles.add(ParagraphStyle(
    'CoverTitle', parent=styles['Title'],
    fontSize=28, leading=34, textColor=TEXT_LIGHT,
    alignment=TA_CENTER, spaceAfter=12, fontName='Helvetica-Bold'
))
styles.add(ParagraphStyle(
    'CoverSubtitle', parent=styles['Normal'],
    fontSize=14, leading=18, textColor=HexColor('#B0C4DE'),
    alignment=TA_CENTER, spaceAfter=6, fontName='Helvetica'
))
styles.add(ParagraphStyle(
    'SectionTitle', parent=styles['Heading1'],
    fontSize=18, leading=22, textColor=DARK_BLUE,
    spaceBefore=16, spaceAfter=10, fontName='Helvetica-Bold'
))
styles.add(ParagraphStyle(
    'SubSection', parent=styles['Heading2'],
    fontSize=14, leading=18, textColor=MID_BLUE,
    spaceBefore=12, spaceAfter=6, fontName='Helvetica-Bold'
))
styles.add(ParagraphStyle(
    'BodyText2', parent=styles['Normal'],
    fontSize=10, leading=14, textColor=TEXT_DARK,
    alignment=TA_JUSTIFY, spaceAfter=6, fontName='Helvetica'
))
styles.add(ParagraphStyle(
    'BulletItem', parent=styles['Normal'],
    fontSize=10, leading=14, textColor=TEXT_DARK,
    leftIndent=20, bulletIndent=8, spaceAfter=3, fontName='Helvetica'
))
styles.add(ParagraphStyle(
    'SmallNote', parent=styles['Normal'],
    fontSize=8, leading=10, textColor=MED_GRAY,
    alignment=TA_CENTER, fontName='Helvetica-Oblique'
))
styles.add(ParagraphStyle(
    'TierHeader', parent=styles['Heading2'],
    fontSize=15, leading=19, textColor=white,
    spaceBefore=0, spaceAfter=0, fontName='Helvetica-Bold',
    alignment=TA_LEFT
))
styles.add(ParagraphStyle(
    'CalloutText', parent=styles['Normal'],
    fontSize=11, leading=15, textColor=TEXT_DARK,
    alignment=TA_LEFT, spaceAfter=4, fontName='Helvetica'
))
styles.add(ParagraphStyle(
    'TOCEntry', parent=styles['Normal'],
    fontSize=11, leading=18, textColor=TEXT_DARK,
    leftIndent=20, fontName='Helvetica'
))
styles.add(ParagraphStyle(
    'TOCTitle', parent=styles['Heading1'],
    fontSize=20, leading=24, textColor=DARK_BLUE,
    spaceBefore=20, spaceAfter=16, fontName='Helvetica-Bold',
    alignment=TA_LEFT
))
styles.add(ParagraphStyle(
    'PageHeader', parent=styles['Normal'],
    fontSize=8, leading=10, textColor=MED_GRAY,
    fontName='Helvetica'
))


class ColorBlock(Flowable):
    """A colored rectangle block for tier headers."""
    def __init__(self, width, height, color, text, style):
        Flowable.__init__(self)
        self.width = width
        self.height = height
        self.color = color
        self.text = text
        self.style = style

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 0, self.width, self.height, 6, fill=1, stroke=0)
        p = Paragraph(self.text, self.style)
        w, h = p.wrap(self.width - 20, self.height)
        p.drawOn(self.canv, 10, (self.height - h) / 2)


class CalloutBox(Flowable):
    """A highlighted callout box."""
    def __init__(self, width, text, border_color, bg_color, style, padding=10):
        Flowable.__init__(self)
        self.width = width
        self.border_color = border_color
        self.bg_color = bg_color
        self.text = text
        self.style = style
        self.padding = padding
        p = Paragraph(self.text, self.style)
        w, h = p.wrap(self.width - 2 * self.padding - 6, 1000)
        self.height = h + 2 * self.padding

    def draw(self):
        self.canv.setFillColor(self.bg_color)
        self.canv.setStrokeColor(self.border_color)
        self.canv.setLineWidth(2)
        self.canv.roundRect(0, 0, self.width, self.height, 4, fill=1, stroke=1)
        # Left accent bar
        self.canv.setFillColor(self.border_color)
        self.canv.rect(0, 0, 6, self.height, fill=1, stroke=0)
        p = Paragraph(self.text, self.style)
        w, h = p.wrap(self.width - 2 * self.padding - 6, self.height)
        p.drawOn(self.canv, self.padding + 6, self.height - h - self.padding)


def header_footer(canvas_obj, doc):
    """Add page header and footer."""
    canvas_obj.saveState()
    page_num = doc.page
    # Skip header/footer on cover page (page 1)
    if page_num > 1:
        # Header line
        canvas_obj.setStrokeColor(SOFT_GRAY)
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(54, letter[1] - 40, letter[0] - 54, letter[1] - 40)
        canvas_obj.setFont('Helvetica', 7)
        canvas_obj.setFillColor(MED_GRAY)
        canvas_obj.drawString(54, letter[1] - 36, "Scorched AI Trading Bot")
        canvas_obj.drawRightString(letter[0] - 54, letter[1] - 36, "Improvement Roadmap")
        # Footer
        canvas_obj.setStrokeColor(SOFT_GRAY)
        canvas_obj.line(54, 40, letter[0] - 54, 40)
        canvas_obj.setFont('Helvetica', 8)
        canvas_obj.setFillColor(MED_GRAY)
        canvas_obj.drawCentredString(letter[0] / 2, 28, f"Page {page_num}")
        canvas_obj.drawString(54, 28, "Confidential")
        canvas_obj.drawRightString(letter[0] - 54, 28, "March 2026")
    canvas_obj.restoreState()


def generate_scatter_chart():
    """Generate the cost/impact scatter chart."""
    items = [
        ("TA Module", 0.00, 9, 'gold'),
        ("Risk Committee", 0.02, 8, 'green'),
        ("Finnhub Consensus", 0.00, 6, 'gold'),
        ("Thinking 16K", 0.02, 5, 'green'),
        ("100+ Universe", 0.05, 7, 'blue'),
        ("Polygon Paid", 0.15, 7, 'blue'),
        ("Position Mgmt", 0.02, 6, 'green'),
        ("Opus Analysis", 0.25, 8, 'orange'),
        ("Earnings Transcripts", 0.10, 7, 'orange'),
        ("Multi-Scenario", 0.05, 6, 'orange'),
        ("Options Activity", 0.15, 7, 'orange'),
        ("Multi-Agent", 0.80, 9, 'red'),
        ("Intraday Monitor", 0.10, 6, 'red'),
        ("Sentiment", 0.05, 5, 'red'),
        ("Backtesting", 0.05, 8, 'green'),
    ]

    color_map = {
        'gold': '#D4A843',
        'green': '#27AE60',
        'blue': '#3498DB',
        'orange': '#E67E22',
        'red': '#E74C3C',
    }
    tier_labels = {
        'gold': 'Free / Compute Only',
        'green': 'Tier 1 ($0.25/day)',
        'blue': 'Tier 2 ($0.50/day)',
        'orange': 'Tier 3 ($1.00/day)',
        'red': 'Tier 4 ($2.50/day)',
    }

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor('#FAFBFD')
    ax.set_facecolor('#FAFBFD')

    # Explicit label positions: (label_x, label_y, ha)
    # Using absolute positions to guarantee no overlaps
    # Labels are connected to dots via thin leader lines
    label_cfg = {
        "TA Module":            (0.08,  9.45, 'left'),
        "Backtesting":          (0.10,  8.45, 'left'),
        "Risk Committee":       (0.08,  7.65, 'left'),
        "Opus Analysis":        (0.32,  8.40, 'left'),
        "Multi-Agent":          (0.62,  9.40, 'left'),
        "100+ Universe":        (0.12,  7.20, 'left'),
        "Earnings Transcripts": (0.20,  7.40, 'left'),
        "Polygon Paid":         (0.22,  6.80, 'left'),
        "Options Activity":     (0.22,  6.40, 'left'),
        "Multi-Scenario":       (0.12,  6.60, 'left'),
        "Position Mgmt":        (0.08,  5.70, 'left'),
        "Finnhub Consensus":    (0.08,  5.30, 'left'),
        "Intraday Monitor":     (0.20,  5.70, 'left'),
        "Thinking 16K":         (0.08,  4.55, 'left'),
        "Sentiment":            (0.12,  4.80, 'left'),
    }

    for name, cost, impact, tier in items:
        c = color_map[tier]
        ax.scatter(cost, impact, s=200, c=c, edgecolors='white',
                   linewidths=1.5, zorder=5, alpha=0.9)
        lx, ly, ha = label_cfg[name]
        ax.annotate(name, (cost, impact),
                    xytext=(lx, ly),
                    textcoords='data',
                    fontsize=7.5, fontweight='medium', color='#2C3E50',
                    ha=ha, va='center',
                    arrowprops=dict(arrowstyle='-', color='#BBBBBB', lw=0.6,
                                    shrinkA=0, shrinkB=3))

    # Legend
    handles = [mpatches.Patch(facecolor=color_map[k], edgecolor='white', label=v)
               for k, v in tier_labels.items()]
    legend = ax.legend(handles=handles, loc='lower right', fontsize=8,
                       framealpha=0.95, edgecolor='#DDD', fancybox=True)

    ax.set_xlabel('Daily Cost ($)', fontsize=11, fontweight='medium', color='#2C3E50')
    ax.set_ylabel('Expected Impact (1-10)', fontsize=11, fontweight='medium', color='#2C3E50')
    ax.set_title('Cost vs. Impact of Potential Improvements', fontsize=14,
                 fontweight='bold', color='#1B2A4A', pad=12)

    ax.set_xlim(-0.05, 0.95)
    ax.set_ylim(4.0, 10.0)
    ax.grid(True, alpha=0.3, linestyle='--')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#CCC')
    ax.spines['bottom'].set_color('#CCC')

    # Quadrant labels
    ax.text(0.02, 9.6, 'HIGH IMPACT + FREE', fontsize=7, color='#27AE60',
            fontweight='bold', alpha=0.6)
    ax.text(0.70, 9.6, 'HIGH IMPACT + PAID', fontsize=7, color='#E74C3C',
            fontweight='bold', alpha=0.6)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=200, bbox_inches='tight',
                facecolor='#FAFBFD', edgecolor='none')
    plt.close()
    buf.seek(0)
    return buf


def generate_cumulative_chart():
    """Generate the cumulative impact bar chart."""
    tiers = ['Current', 'Tier 1\n$0.12/day', 'Tier 2\n$0.35/day', 'Tier 3\n$0.70/day', 'Tier 4\n$1.50/day']
    costs = [0.08, 0.12, 0.35, 0.70, 1.50]
    num_improvements = [0, 4, 7, 11, 15]
    colors_bars = ['#9BA3AF', '#27AE60', '#3498DB', '#E67E22', '#E74C3C']

    fig, ax1 = plt.subplots(figsize=(8, 4.5))
    fig.patch.set_facecolor('#FAFBFD')
    ax1.set_facecolor('#FAFBFD')

    x = np.arange(len(tiers))
    width = 0.35

    bars1 = ax1.bar(x - width/2, costs, width, color=colors_bars, edgecolor='white',
                    linewidth=1.5, alpha=0.85, label='Daily Cost ($)')
    ax1.set_ylabel('Daily Cost ($)', fontsize=10, color='#2C3E50', fontweight='medium')
    ax1.set_ylim(0, 2.0)

    ax2 = ax1.twinx()
    bars2 = ax2.bar(x + width/2, num_improvements, width, color=[c + '66' for c in
                    ['#9BA3AF', '#27AE60', '#3498DB', '#E67E22', '#E74C3C']],
                    edgecolor=colors_bars, linewidth=1.5, label='Improvements (#)')
    ax2.set_ylabel('Cumulative Improvements', fontsize=10, color='#2C3E50', fontweight='medium')
    ax2.set_ylim(0, 18)

    # Add value labels
    for bar, val in zip(bars1, costs):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.03,
                 f'${val:.2f}', ha='center', va='bottom', fontsize=8, fontweight='bold',
                 color='#2C3E50')
    for bar, val in zip(bars2, num_improvements):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                 str(val), ha='center', va='bottom', fontsize=8, fontweight='bold',
                 color='#2C3E50')

    ax1.set_xticks(x)
    ax1.set_xticklabels(tiers, fontsize=9)
    ax1.set_title('Cumulative Cost and Improvements by Tier', fontsize=14,
                  fontweight='bold', color='#1B2A4A', pad=12)

    ax1.spines['top'].set_visible(False)
    ax2.spines['top'].set_visible(False)
    ax1.spines['left'].set_color('#CCC')
    ax1.spines['bottom'].set_color('#CCC')
    ax2.spines['right'].set_color('#CCC')
    ax1.grid(True, axis='y', alpha=0.2, linestyle='--')

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=8,
               framealpha=0.9, edgecolor='#DDD')

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=200, bbox_inches='tight',
                facecolor='#FAFBFD', edgecolor='none')
    plt.close()
    buf.seek(0)
    return buf


def generate_pipeline_diagram():
    """Generate LLM architecture comparison diagram."""
    fig, axes = plt.subplots(1, 3, figsize=(10, 4.2))
    fig.patch.set_facecolor('#FAFBFD')

    configs = [
        {
            'title': 'Current (2 Calls)',
            'boxes': [
                ('Research\nData', '#4A90D9', 0.85),
                ('Call 1: Analysis\n(Sonnet, thinking)', '#1B2A4A', 0.60),
                ('Call 2: Decision\n(Sonnet)', '#1B2A4A', 0.35),
                ('Recommendations', '#27AE60', 0.10),
            ],
            'cost': '$0.08/day'
        },
        {
            'title': 'Tier 1 (3 Calls)',
            'boxes': [
                ('Research\nData', '#4A90D9', 0.85),
                ('Call 1: Analysis\n(Sonnet, thinking)', '#1B2A4A', 0.63),
                ('Call 2: Decision\n(Sonnet)', '#1B2A4A', 0.43),
                ('Call 3: Risk Review\n(Adversarial)', '#E67E22', 0.23),
                ('Recommendations', '#27AE60', 0.03),
            ],
            'cost': '$0.12/day'
        },
        {
            'title': 'Tier 4 (Multi-Agent)',
            'boxes': [
                ('Research Data', '#4A90D9', 0.85),
                ('Macro\nAnalyst', '#2E4A7A', 0.63),
                ('Stock\nAnalyst', '#2E4A7A', 0.63),
                ('Portfolio\nManager', '#1B2A4A', 0.40),
                ('Risk\nCommittee', '#E67E22', 0.20),
                ('Execution', '#27AE60', 0.00),
            ],
            'cost': '$1.50/day',
            'parallel': True
        },
    ]

    for idx, (ax, cfg) in enumerate(zip(axes, configs)):
        ax.set_facecolor('#FAFBFD')
        ax.set_xlim(0, 1)
        ax.set_ylim(-0.05, 1.05)
        ax.axis('off')
        ax.set_title(cfg['title'], fontsize=10, fontweight='bold', color='#1B2A4A', pad=8)

        boxes = cfg['boxes']
        is_parallel = cfg.get('parallel', False)

        for i, (label, color, y) in enumerate(boxes):
            if is_parallel and i in (1, 2):
                # Side by side
                w = 0.38
                x_pos = 0.08 if i == 1 else 0.54
            else:
                w = 0.84
                x_pos = 0.08
            h = 0.14
            rect = FancyBboxPatch((x_pos, y), w, h, boxstyle="round,pad=0.02",
                                  facecolor=color, edgecolor='white', linewidth=1.5)
            ax.add_patch(rect)
            ax.text(x_pos + w/2, y + h/2, label, ha='center', va='center',
                    fontsize=7, color='white', fontweight='bold')

            # Draw arrows between sequential boxes
            if i > 0 and not (is_parallel and i == 2):
                prev_y = boxes[i-1][2]
                prev_h = 0.14
                if is_parallel and i == 3:
                    # Arrow from both parallel boxes
                    ax.annotate('', xy=(0.5, y + h), xytext=(0.27, boxes[1][2]),
                                arrowprops=dict(arrowstyle='->', color='#9BA3AF', lw=1.2))
                    ax.annotate('', xy=(0.5, y + h), xytext=(0.73, boxes[2][2]),
                                arrowprops=dict(arrowstyle='->', color='#9BA3AF', lw=1.2))
                else:
                    ax.annotate('', xy=(0.5, y + h), xytext=(0.5, prev_y),
                                arrowprops=dict(arrowstyle='->', color='#9BA3AF', lw=1.2))

        ax.text(0.5, -0.04, cfg['cost'], ha='center', va='top', fontsize=9,
                fontweight='bold', color='#E67E22')

    plt.suptitle('LLM Pipeline Architecture Comparison', fontsize=13,
                 fontweight='bold', color='#1B2A4A', y=1.02)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=200, bbox_inches='tight',
                facecolor='#FAFBFD', edgecolor='none')
    plt.close()
    buf.seek(0)
    return buf


def build_pdf():
    """Build the complete PDF document."""
    doc = SimpleDocTemplate(
        OUTPUT_PATH,
        pagesize=letter,
        leftMargin=54, rightMargin=54,
        topMargin=54, bottomMargin=54
    )

    story = []
    usable_width = letter[0] - 108  # 54 margin each side

    # ═══════════════════════════════════════
    # COVER PAGE
    # ═══════════════════════════════════════
    story.append(Spacer(1, 100))

    # Title block using a table with background
    cover_data = [[
        Paragraph("Scorched AI Trading Bot", styles['CoverTitle']),
    ], [
        Paragraph("Improvement Roadmap for Live Trading", ParagraphStyle(
            'CoverSub2', parent=styles['CoverSubtitle'], fontSize=16, leading=20,
            textColor=ACCENT_GOLD
        )),
    ], [
        Spacer(1, 20),
    ], [
        Paragraph("From $0.08/day to Institutional Quality", styles['CoverSubtitle']),
    ], [
        Spacer(1, 8),
    ], [
        Paragraph("A tiered investment plan for maximizing trading performance", styles['CoverSubtitle']),
    ]]

    cover_table = Table(cover_data, colWidths=[usable_width])
    cover_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), DARK_BLUE),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 20),
        ('RIGHTPADDING', (0, 0), (-1, -1), 20),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(cover_table)

    story.append(Spacer(1, 40))

    # Info block
    info_data = [
        [Paragraph('<b>Prepared for:</b>', styles['BodyText2']),
         Paragraph('Scorched AI Trading Bot', styles['BodyText2'])],
        [Paragraph('<b>Date:</b>', styles['BodyText2']),
         Paragraph('March 28, 2026', styles['BodyText2'])],
        [Paragraph('<b>Current Capital:</b>', styles['BodyText2']),
         Paragraph('$100,343 ($100K starting)', styles['BodyText2'])],
        [Paragraph('<b>Current Daily Cost:</b>', styles['BodyText2']),
         Paragraph('$0.08/day ($2.37/month)', styles['BodyText2'])],
        [Paragraph('<b>Recommendation:</b>', styles['BodyText2']),
         Paragraph('<b>Tier 2 ($0.50/day / $10 per month)</b>', ParagraphStyle(
             'RecBold', parent=styles['BodyText2'], textColor=TIER_BLUE
         ))],
    ]
    info_table = Table(info_data, colWidths=[usable_width * 0.35, usable_width * 0.65])
    info_table.setStyle(TableStyle([
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -2), 0.5, SOFT_GRAY),
    ]))
    story.append(info_table)

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # TABLE OF CONTENTS
    # ═══════════════════════════════════════
    story.append(Paragraph("Table of Contents", styles['TOCTitle']))
    story.append(HRFlowable(width="100%", thickness=1, color=DARK_BLUE))
    story.append(Spacer(1, 12))

    toc_items = [
        ("1.", "Current Performance Summary"),
        ("2.", "Cost vs. Impact Analysis"),
        ("3.", "Tier 1: Better Decisions ($0.25/day)"),
        ("4.", "Tier 2: See More, Know More ($0.50/day)"),
        ("5.", "Tier 3: Institutional Quality ($1.00/day)"),
        ("6.", "Tier 4: Multi-Agent System ($2.50/day)"),
        ("7.", "Tier 5: Institutional Grade (>$5/day)"),
        ("8.", "Cumulative Impact Overview"),
        ("9.", "Data Source Comparison"),
        ("10.", "LLM Architecture Comparison"),
        ("11.", "Recommendation"),
    ]
    for num, title in toc_items:
        story.append(Paragraph(f'<b>{num}</b>  {title}', styles['TOCEntry']))

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # 1. CURRENT PERFORMANCE SUMMARY
    # ═══════════════════════════════════════
    story.append(Paragraph("1. Current Performance Summary", styles['SectionTitle']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BLUE))
    story.append(Spacer(1, 8))

    story.append(Paragraph(
        "After 30 days of live trading with a $100,000 paper portfolio, the bot has produced "
        "a modest positive return of +0.34%, with a portfolio value of $100,343. While the "
        "unrealized positions are all profitable, the closed trade record reveals structural "
        "weaknesses that can be addressed systematically.",
        styles['BodyText2']
    ))
    story.append(Spacer(1, 8))

    # Performance metrics table
    perf_data = [
        [Paragraph('<b>Metric</b>', styles['BodyText2']),
         Paragraph('<b>Value</b>', styles['BodyText2']),
         Paragraph('<b>Assessment</b>', styles['BodyText2'])],
        ['30-Day Return', '+0.34% ($100,343)', 'Slightly positive'],
        ['Closed Trades', '12 (5W / 7L)', 'Below breakeven rate'],
        ['Win Rate', '42%', 'Needs improvement (target: 55%+)'],
        ['Avg Win / Avg Loss', '+$466 / -$638', 'Reward/risk ratio: 0.73'],
        ['Realized P&L', '-$2,136', 'Losses from thesis failures'],
        ['Unrealized P&L', '+$2,479 (5 positions)', 'All open positions green'],
        ['Daily Cost', '$0.08/day', 'Extremely cost-efficient'],
        ['Biggest Losses', 'FCX (-$1,681), ORCL (-$978)', 'Thesis identification failures'],
    ]

    perf_table = Table(perf_data, colWidths=[usable_width*0.25, usable_width*0.35, usable_width*0.40])
    perf_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
        ('ROUNDEDCORNERS', [4, 4, 4, 4]),
    ])
    perf_table.setStyle(perf_style)
    story.append(perf_table)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Current Cost Breakdown", styles['SubSection']))
    cost_data = [
        [Paragraph('<b>Component</b>', styles['BodyText2']),
         Paragraph('<b>Daily Cost</b>', styles['BodyText2']),
         Paragraph('<b>Monthly</b>', styles['BodyText2'])],
        ['Call 1: Analysis (Sonnet + thinking)', '$0.054', '$1.62'],
        ['Call 2: Decision (Sonnet)', '$0.012', '$0.36'],
        ['Call 3: EOD Review (Sonnet)', '$0.013', '$0.39'],
        [Paragraph('<b>Total</b>', styles['BodyText2']), Paragraph('<b>$0.079</b>', styles['BodyText2']),
         Paragraph('<b>$2.37</b>', styles['BodyText2'])],
    ]
    cost_table = Table(cost_data, colWidths=[usable_width*0.50, usable_width*0.25, usable_width*0.25])
    cost_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), MID_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
        ('BACKGROUND', (0, -1), (-1, -1), HexColor('#E8F4FD')),
    ]))
    story.append(cost_table)
    story.append(Spacer(1, 10))

    # Current gaps callout
    story.append(CalloutBox(
        usable_width,
        '<b>Key Gaps Identified:</b><br/>'
        '&bull; No technical analysis beyond RSI (no MACD, Bollinger Bands, support/resistance)<br/>'
        '&bull; No analyst consensus or price targets<br/>'
        '&bull; No sentiment data from social media or news<br/>'
        '&bull; Single LLM pass with no adversarial review (thesis failures go unchallenged)<br/>'
        '&bull; 40-stock watchlist limits opportunity discovery<br/>'
        '&bull; Headlines only from news sources (no full article context)<br/>'
        '&bull; 15-minute delayed price data via yfinance',
        ACCENT_ORANGE, HexColor('#FFF8F0'), styles['CalloutText']
    ))

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # 2. COST vs IMPACT SCATTER
    # ═══════════════════════════════════════
    story.append(Paragraph("2. Cost vs. Impact Analysis", styles['SectionTitle']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BLUE))
    story.append(Spacer(1, 6))

    story.append(Paragraph(
        "The scatter chart below maps each potential improvement by its daily cost and expected "
        "impact on trading performance. The upper-left quadrant represents the highest-value "
        "improvements: high impact at zero or minimal cost. Color indicates which tier "
        "includes each improvement.",
        styles['BodyText2']
    ))
    story.append(Spacer(1, 6))

    scatter_buf = generate_scatter_chart()
    scatter_img = Image(scatter_buf, width=usable_width, height=usable_width * 0.6)
    story.append(scatter_img)
    story.append(Spacer(1, 8))

    story.append(CalloutBox(
        usable_width,
        '<b>Key Insight:</b> The technical analysis module (MACD, Bollinger Bands, support/resistance) '
        'and backtesting framework are the highest-impact improvements at zero marginal cost. '
        'They require only compute time, not API spend. The risk committee call at $0.02/day '
        'is the single best dollar-for-dollar investment, directly addressing the #1 loss pattern.',
        DARK_BLUE, HexColor('#EEF2F7'), styles['CalloutText']
    ))

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # 3-7. TIER BREAKDOWN PAGES
    # ═══════════════════════════════════════
    tiers = [
        {
            'num': 3, 'name': 'Tier 1: Better Decisions',
            'cost': '$0.25/day ($5/month)', 'effective': '$0.12/day effective',
            'color': TIER_GREEN,
            'items': [
                ('Technical Analysis Module', '$0.00/day',
                 'Add MACD, Bollinger Bands, and support/resistance levels computed locally. '
                 'Fills the biggest analytical gap at zero cost. Claude currently makes decisions '
                 'with only RSI -- like a doctor diagnosing with only temperature.'),
                ('Risk Committee Call', '$0.02/day',
                 'A third Claude call that plays devil\'s advocate, actively trying to poke holes '
                 'in the trading thesis. Could have prevented both FCX (-$1,681) and ORCL (-$978) '
                 'by challenging weak catalysts.'),
                ('Finnhub Analyst Consensus', '$0.00/day',
                 'Free API providing analyst price targets and consensus ratings. Adds a '
                 '"Wall Street sanity check" to every recommendation.'),
                ('Increase Thinking Budget', '$0.02/day',
                 'Double the extended thinking budget from 8K to 16K tokens. Gives the analysis '
                 'call more room for deeper reasoning on complex setups.'),
            ],
            'message': (
                'This tier adds a skeptical second opinion and free data sources. '
                'The risk committee alone could have prevented the two biggest losses '
                '(FCX + ORCL = -$2,659 combined), which exceeds the entire 30-day cost of '
                'running this tier for over 5 years.'
            ),
        },
        {
            'num': 4, 'name': 'Tier 2: See More, Know More',
            'cost': '$0.50/day ($10/month)', 'effective': '$0.35/day effective',
            'color': TIER_BLUE,
            'items': [
                ('Everything in Tier 1', '--', 'All Tier 1 improvements carry forward.'),
                ('Expand Universe to 100+ Stocks', '$0.05/day',
                 'Widen the momentum screener beyond the current 40-stock watchlist. '
                 'The bot\'s best trades (HAL, CF, SLB) came from the screener, not the watchlist.'),
                ('Polygon Paid Tier', '$0.15/day',
                 'Full article text instead of headlines only. Claude currently guesses article '
                 'content from titles -- like reading a book by its cover.'),
                ('Position Management Call', '$0.02/day',
                 'An EOD call focused on open positions: should stops be tightened? '
                 'Has the thesis changed? Proactive instead of reactive.'),
            ],
            'message': (
                'A wider opportunity set combined with proactive position management. '
                'The best trades consistently came from the screener discovering stocks '
                'the watchlist missed. Full article text transforms Claude\'s news analysis '
                'from headline speculation to informed assessment.'
            ),
        },
        {
            'num': 5, 'name': 'Tier 3: Institutional Quality Analysis',
            'cost': '$1.00/day ($20/month)', 'effective': '$0.70/day effective',
            'color': TIER_ORANGE,
            'items': [
                ('Everything in Tier 2', '--', 'All Tier 1 + Tier 2 improvements carry forward.'),
                ('Upgrade Call 1 to Opus', '$0.25/day',
                 'Opus-quality reasoning for the analysis call catches thesis errors that Sonnet '
                 'misses. The analysis call is where conviction is formed -- it deserves the '
                 'strongest model.'),
                ('Earnings Call Transcripts', '$0.10/day',
                 'Management commentary from recent earnings calls. Leading indicator of '
                 'fundamental direction that price data alone cannot capture.'),
                ('Multi-Scenario Simulation', '$0.05/day',
                 'Run bull/bear/base scenarios for each candidate. Forces explicit consideration '
                 'of downside risk before entering positions.'),
                ('Unusual Options Activity', '$0.15/day',
                 'Smart money flow data. Large unusual options trades often precede significant '
                 'price moves by 1-5 days -- exactly the bot\'s holding period.'),
            ],
            'message': (
                'Opus-quality reasoning catches thesis errors that Sonnet misses. '
                'Earnings transcripts and options flow are leading indicators -- they tell '
                'you what is about to happen, not what already happened. This tier transforms '
                'the bot from reactive to predictive.'
            ),
        },
        {
            'num': 6, 'name': 'Tier 4: Multi-Agent System',
            'cost': '$2.50/day ($50/month)', 'effective': '$1.50/day effective',
            'color': TIER_RED,
            'items': [
                ('Everything in Tier 3', '--', 'All Tier 1-3 improvements carry forward.'),
                ('Full Multi-Agent Pipeline', '$0.80/day',
                 'Three specialized agents: Macro Analyst (rates, sector rotation, risk-on/off), '
                 'Stock Analyst (fundamentals, technicals, catalysts), Portfolio Manager '
                 '(sizing, correlation, risk budget). Each excels at their domain.'),
                ('Intraday Monitoring', '$0.10/day',
                 '11 AM and 2 PM check-in calls. Prevents holding through flash crashes or '
                 'sudden reversals. Enables same-day exits when thesis breaks.'),
                ('Sentiment Analysis', '$0.05/day',
                 'Social media and news sentiment scoring. Identifies crowded trades and '
                 'contrarian opportunities.'),
                ('Backtesting Framework', '$0.05/day (amortized)',
                 'Run strategy variants against historical data. Validates ideas before risking '
                 'real capital. The only way to distinguish skill from luck.'),
            ],
            'message': (
                'Specialized agents each excel at their domain rather than one generalist '
                'handling everything. Intraday monitoring prevents holding through crashes. '
                'The backtesting framework provides the feedback loop needed to continuously '
                'improve strategy parameters.'
            ),
        },
        {
            'num': 7, 'name': 'Tier 5: Institutional Grade',
            'cost': '>$5/day ($150+/month)', 'effective': 'Variable',
            'color': TIER_PURPLE,
            'items': [
                ('Opus Everywhere', '$2-5/day',
                 'Use Opus for all calls including decision, risk review, and position management.'),
                ('Bloomberg Terminal API', '$65/day',
                 'The gold standard for financial data. Real-time, comprehensive, and expensive. '
                 'Only makes sense at $50K+ capital.'),
                ('Alternative Data', '$2-15/day',
                 'Satellite imagery, credit card data, web traffic analytics. '
                 'Genuine information edge, but expensive.'),
                ('Full Russell 3000 Universe', '$1-3/day',
                 'Screen the entire US equity market. Maximum opportunity discovery at '
                 'maximum compute cost.'),
            ],
            'message': (
                'This tier only makes economic sense at $50,000+ in capital. '
                'Bloomberg alone costs $65/day -- 24 times the value of a $1,000 trading account '
                'annually. File under "aspirational" until the portfolio justifies it.'
            ),
        },
    ]

    for tier in tiers:
        story.append(Paragraph(f"{tier['num']}. {tier['name']}", styles['SectionTitle']))
        story.append(ColorBlock(usable_width, 30, tier['color'],
                                f"&nbsp;&nbsp;{tier['cost']}  |  {tier['effective']}",
                                styles['TierHeader']))
        story.append(Spacer(1, 8))

        # Items table
        tier_data = [
            [Paragraph('<b>Improvement</b>', styles['BodyText2']),
             Paragraph('<b>Cost</b>', styles['BodyText2']),
             Paragraph('<b>Details</b>', styles['BodyText2'])],
        ]
        for name, cost, desc in tier['items']:
            tier_data.append([
                Paragraph(f'<b>{name}</b>', styles['BodyText2']),
                Paragraph(cost, styles['BodyText2']),
                Paragraph(desc, styles['BodyText2']),
            ])

        tier_table = Table(tier_data, colWidths=[usable_width*0.22, usable_width*0.12, usable_width*0.66])
        tier_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), tier['color']),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
            ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(tier_table)
        story.append(Spacer(1, 8))

        story.append(CalloutBox(
            usable_width,
            f'<b>Key Message:</b> {tier["message"]}',
            tier['color'], HexColor('#F8F9FA'), styles['CalloutText']
        ))

        story.append(PageBreak())

    # ═══════════════════════════════════════
    # 8. CUMULATIVE IMPACT
    # ═══════════════════════════════════════
    story.append(Paragraph("8. Cumulative Impact Overview", styles['SectionTitle']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BLUE))
    story.append(Spacer(1, 8))

    story.append(Paragraph(
        "Each tier builds upon the previous, creating a compounding effect. The chart below "
        "shows how daily cost and the number of active improvements scale together across tiers.",
        styles['BodyText2']
    ))
    story.append(Spacer(1, 6))

    cum_buf = generate_cumulative_chart()
    cum_img = Image(cum_buf, width=usable_width * 0.9, height=usable_width * 0.5)
    story.append(cum_img)
    story.append(Spacer(1, 12))

    # Summary table
    sum_data = [
        [Paragraph('<b>Tier</b>', styles['BodyText2']),
         Paragraph('<b>Daily Cost</b>', styles['BodyText2']),
         Paragraph('<b>Monthly</b>', styles['BodyText2']),
         Paragraph('<b>Annual</b>', styles['BodyText2']),
         Paragraph('<b>% of $100K Capital</b>', styles['BodyText2'])],
        ['Current', '$0.08', '$2.37', '$29', '0.03%'],
        ['Tier 1', '$0.12', '$3.60', '$44', '0.04%'],
        ['Tier 2', '$0.35', '$10.50', '$128', '0.13%'],
        ['Tier 3', '$0.70', '$21.00', '$256', '0.26%'],
        ['Tier 4', '$1.50', '$45.00', '$548', '0.55%'],
    ]
    sum_table = Table(sum_data, colWidths=[usable_width*0.18, usable_width*0.18,
                                           usable_width*0.18, usable_width*0.18, usable_width*0.28])
    sum_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
        ('BACKGROUND', (0, 3), (-1, 3), HexColor('#E8F4FD')),
    ]))
    story.append(sum_table)

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # 9. DATA SOURCE COMPARISON
    # ═══════════════════════════════════════
    story.append(Paragraph("9. Data Source Comparison", styles['SectionTitle']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BLUE))
    story.append(Spacer(1, 8))

    story.append(Paragraph(
        "The table below shows what data Claude has access to at each tier. "
        "Each tier is cumulative -- all capabilities from lower tiers carry forward.",
        styles['BodyText2']
    ))
    story.append(Spacer(1, 6))

    check = '<font color="#27AE60"><b>YES</b></font>'
    cross = '<font color="#CC0000">--</font>'

    ds_data = [
        ['', 'Current', 'Tier 1', 'Tier 2', 'Tier 3', 'Tier 4'],
        ['Price Data (delayed)', check, check, check, check, check],
        ['Fundamentals', check, check, check, check, check],
        ['News Headlines', check, check, check, check, check],
        ['Full Article Text', cross, cross, check, check, check],
        ['Macro Indicators (FRED)', check, check, check, check, check],
        ['RSI (14)', check, check, check, check, check],
        ['Full Technicals (MACD/BB/S-R)', cross, check, check, check, check],
        ['Analyst Consensus', cross, check, check, check, check],
        ['Earnings Transcripts', cross, cross, cross, check, check],
        ['Unusual Options Flow', cross, cross, cross, check, check],
        ['Sentiment (Social/News)', cross, cross, cross, cross, check],
        ['Real-Time Data', cross, cross, cross, cross, cross],
    ]

    # Convert strings to Paragraphs
    ds_formatted = []
    for row in ds_data:
        ds_formatted.append([
            Paragraph(f'<b>{row[0]}</b>' if row[0] else '', styles['BodyText2'])
        ] + [Paragraph(cell, ParagraphStyle('DSCell', parent=styles['BodyText2'],
                                            alignment=TA_CENTER, fontSize=9))
             for cell in row[1:]])

    cw = usable_width / 6
    ds_table = Table(ds_formatted, colWidths=[cw * 1.8, cw * 0.84, cw * 0.84, cw * 0.84, cw * 0.84, cw * 0.84])
    ds_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
        ('LINEBEFORE', (1, 0), (-1, -1), 0.3, MED_GRAY),
    ]))
    story.append(ds_table)

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # 10. LLM ARCHITECTURE DIAGRAM
    # ═══════════════════════════════════════
    story.append(Paragraph("10. LLM Architecture Comparison", styles['SectionTitle']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BLUE))
    story.append(Spacer(1, 8))

    story.append(Paragraph(
        "The current two-call pipeline handles everything in a single analysis pass with "
        "no adversarial review. Tier 1 adds a risk committee call that challenges the thesis "
        "before execution. Tier 4 implements a full multi-agent system with specialized roles.",
        styles['BodyText2']
    ))
    story.append(Spacer(1, 6))

    pipe_buf = generate_pipeline_diagram()
    pipe_img = Image(pipe_buf, width=usable_width, height=usable_width * 0.42)
    story.append(pipe_img)
    story.append(Spacer(1, 12))

    # Architecture details table
    arch_data = [
        ['', 'Current', 'Tier 1', 'Tier 4'],
        ['Claude Calls/Day', '3', '4', '8-10'],
        ['Models Used', 'Sonnet only', 'Sonnet only', 'Opus + Sonnet'],
        ['Adversarial Review', 'None', 'Risk Committee', 'Full Committee'],
        ['Specialization', 'Generalist', 'Generalist + Skeptic', '3 Specialists'],
        ['Position Monitoring', 'EOD only', 'EOD only', 'Intraday (3x)'],
        ['Estimated Daily Cost', '$0.08', '$0.12', '$1.50'],
    ]
    arch_formatted = []
    for row in arch_data:
        arch_formatted.append([
            Paragraph(f'<b>{row[0]}</b>' if row[0] else '', styles['BodyText2'])
        ] + [Paragraph(cell, ParagraphStyle('ArchCell', parent=styles['BodyText2'],
                                            alignment=TA_CENTER, fontSize=9))
             for cell in row[1:]])

    aw = usable_width / 4
    arch_table = Table(arch_formatted, colWidths=[aw * 1.2, aw * 0.93, aw * 0.93, aw * 0.93])
    arch_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), DARK_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
        ('LINEBEFORE', (1, 0), (-1, -1), 0.3, MED_GRAY),
    ]))
    story.append(arch_table)

    story.append(PageBreak())

    # ═══════════════════════════════════════
    # 11. RECOMMENDATION
    # ═══════════════════════════════════════
    story.append(Paragraph("11. Recommendation", styles['SectionTitle']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=LIGHT_BLUE))
    story.append(Spacer(1, 12))

    # Big recommendation callout
    rec_data = [[
        Paragraph(
            '<font size="18" color="#FFFFFF"><b>RECOMMENDED: Tier 2</b></font><br/>'
            '<font size="13" color="#B0C4DE">$0.50/day  |  $10/month  |  $128/year</font>',
            ParagraphStyle('RecTitle', parent=styles['CoverTitle'], fontSize=18, leading=28,
                          alignment=TA_CENTER)
        )
    ]]
    rec_table = Table(rec_data, colWidths=[usable_width])
    rec_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), TIER_BLUE),
        ('TOPPADDING', (0, 0), (-1, -1), 16),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 16),
        ('LEFTPADDING', (0, 0), (-1, -1), 20),
        ('RIGHTPADDING', (0, 0), (-1, -1), 20),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(rec_table)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Why Tier 2?", styles['SubSection']))
    story.append(Spacer(1, 4))

    reasons = [
        ('<b>Technical analysis module is FREE</b> and fills the single biggest analytical gap. '
         'MACD, Bollinger Bands, and support/resistance levels are computed locally with zero API cost.'),
        ('<b>Risk committee prevents thesis failures</b> -- the #1 loss pattern. An adversarial '
         'third call at $0.02/day could have saved $2,659 in the first month alone.'),
        ('<b>Expanded universe catches more opportunities.</b> The bot\'s best trades (HAL +$891, '
         'CF +$445, SLB +$367) all came from the momentum screener, not the static watchlist.'),
        ('<b>Polygon full articles give Claude real context</b> instead of guessing article content '
         'from headlines. At $0.15/day, this transforms news analysis from speculation to assessment.'),
        ('<b>Analyst consensus adds a Wall Street sanity check</b> at zero cost via Finnhub free tier.'),
    ]
    for reason in reasons:
        story.append(Paragraph(f'&bull;&nbsp;&nbsp;{reason}', styles['BulletItem']))
        story.append(Spacer(1, 2))

    story.append(Spacer(1, 12))

    # ROI callout
    story.append(CalloutBox(
        usable_width,
        '<b>Return on Investment:</b><br/><br/>'
        'Annual cost of Tier 2: <b>$128</b><br/>'
        'Cost as % of $100K capital: <b>0.13%</b><br/>'
        'FCX + ORCL losses (preventable): <b>$2,659</b><br/>'
        'Breakeven: Tier 2 pays for itself if it prevents <b>one bad trade per year</b>.<br/><br/>'
        'At scale ($50K+ capital), even Tier 3 at $256/year represents just 0.5% of capital -- '
        'well below the 1-2% expense ratio of most actively managed funds.',
        TIER_GREEN, HexColor('#F0FAF4'), styles['CalloutText']
    ))

    story.append(Spacer(1, 16))

    # Implementation timeline
    story.append(Paragraph("Suggested Implementation Timeline", styles['SubSection']))
    story.append(Spacer(1, 4))

    timeline_data = [
        [Paragraph('<b>Week</b>', styles['BodyText2']),
         Paragraph('<b>Action</b>', styles['BodyText2']),
         Paragraph('<b>Expected Result</b>', styles['BodyText2'])],
        ['Week 1', 'Implement technical analysis module (free)',
         'MACD, BB, S/R levels in every analysis'],
        ['Week 2', 'Add risk committee call + Finnhub consensus',
         'Adversarial review catches weak theses'],
        ['Week 3', 'Expand universe + add position management',
         'Wider opportunity set, tighter stop management'],
        ['Week 4', 'Upgrade to Polygon paid tier',
         'Full article text improves news analysis'],
        ['Week 5+', 'Monitor results and evaluate Tier 3',
         'Data-driven decision on further investment'],
    ]

    tl_table = Table(timeline_data, colWidths=[usable_width*0.12, usable_width*0.44, usable_width*0.44])
    tl_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), MID_BLUE),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, SOFT_GRAY]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, MED_GRAY),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(tl_table)

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=DARK_BLUE))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Generated March 28, 2026  |  Scorched AI Trading Bot  |  Confidential",
        styles['SmallNote']
    ))

    # Build the PDF
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    print(f"PDF generated: {OUTPUT_PATH}")
    return OUTPUT_PATH


if __name__ == '__main__':
    build_pdf()
