"""Measure TUI history-update cost without opening a terminal, bot or database."""
import argparse
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xgent_app.cli_render import MessageRenderer, Palette
from xgent_app.cli_tui import MessageModel
from xgent_app.protocols import ProtocolParser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--messages', type=int, nargs='+', default=[50, 300, 1000])
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    if args.repeats < 1 or any(count < 1 for count in args.messages):
        parser.error('counts and repeats must be positive')
    body = '\n'.join(f'output line {i}' for i in range(55))
    protocol = f'```run-x\n<<BEGIN_BENCH1234\n{body}\n<<END_BENCH1234\n```'
    html = ProtocolParser.render_folded_html(protocol, prose_renderer=lambda text: text)
    lines = MessageRenderer(Palette(True), 100).render_text(html, 'HTML')
    print('Messages  Rows   Cold flatten ms  Median update ms  Max update ms')
    for count in args.messages:
        model = MessageModel(Palette(True))
        for index in range(count):
            model.upsert(index, lines)
        started = time.perf_counter()
        rows = model.render_rows()
        cold_ms = (time.perf_counter() - started) * 1000
        samples = []
        for index in range(args.repeats):
            started = time.perf_counter()
            model.upsert(count - 1, lines + [f'new stream tail {index}'])
            rows = model.render_rows()
            model.counts()
            samples.append((time.perf_counter() - started) * 1000)
        print(f'{count:8d} {len(rows):5d} {cold_ms:16.3f} '
              f'{statistics.median(samples):17.3f} {max(samples):14.3f}')
    print('Measures CPU layout work only, not terminal I/O or model latency.')


if __name__ == '__main__':
    main()
