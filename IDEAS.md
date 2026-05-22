# Helm — Ideas for Future Development

A living brainstorm for where Helm could go next. Organized by theme, not priority.

---

## Visualization & Exploration

### Stock-o-Verse (3D Market Explorer)
A Three.js command center where the trader flies through a latent vector space of the entire market — companies clustered and positioned by sector, market cap, volatility, age, and other embeddings. Think spaceship navigating a constellation of stocks. The premise: discovery is broken in every other tool. Scrolling a flat list is not how you find your next position. The embedding space makes relationships visible that a screener never could.

**Axes to cluster by:** sector, net cap, volatility regime, age, institutional ownership, sentiment drift, momentum percentile.

**Interactions:** fly-to on click, pull up a mini-dashboard for any node, draw selection regions to define watchlists, color-map by any live feed (sentiment, congressional flow, insider activity).

**Zero reason not to use Three.js here.** This is exactly the kind of view it was built for.

### Portfolio Biome Map *(Claude)*
A treemap-of-treemaps where each holding is a tile sized by exposure, colored by current sentiment from a toggleable exotic feed (HN / r/WSB / congressional flow / fear & greed), and bordered by realized vol. Click a tile, it explodes into a tiled mini-dashboard for that ticker using existing exotic widgets — no reimplementation, just dockview re-layouts pre-filtered. Plays directly on Helm's tiling DNA and uses all nine exotic feeds in concert instead of one at a time.

### Macro Chord-Progression Viewer *(Claude)*
Treat trading regimes — risk-on, risk-off, hawkish surprise, growth scare, dollar dominance — as chords, with the current one as a "now playing" panel. A horizontal timeline below shows the last N regime transitions and the econ-calendar event that triggered each (CPI prints, FOMC, NFP — already in the feed). A sidebar lists which saved `BacktestResult`s historically Sharpe-best in the current regime. Regime inference starts as k-means on (VIX, 10y–2y slope, DXY momentum, S&P breadth) and graduates to an HMM. The only idea here that needs a real ML model; the rest are mostly plumbing.

---

## Strategy Development

### Strategy Cooking Lab
A panes-and-windows interface for devising, composing, and backtesting trading strategies. Core features:

- **Signal composer:** drag-and-drop signals with configurable weights, timing offsets, and predictive delay estimates
- **Correlation inspector:** see how correlated each signal is with price movement and at what lead/lag
- **If-else logic builder:** visual representation of signals playing against each other (e.g. "enter only if momentum AND sentiment agree but volume is below threshold")
- **Live Claude terminal:** bounce ideas off the model in context — it sees your current signal stack, the backtest results, and the feed data
- **Portable strategy modules:** each strategy is a self-contained file you can version, share, and load into any Helm instance

### Bayesian Decision-Making Sandbox
A [ReactFlow](https://reactflow.dev/) flowgraph where nodes are priors, indicators, and market signals that feed into a probability estimate that a stock is undervalued (or a trade is favorable). Each node has a configurable weight and confidence interval. The graph computes a posterior in real time as live data flows through it. Think visual Bayesian network meets trading thesis builder — start with "earnings beat likely" and "insider buying" as priors, wire them together, and watch the posterior update tick by tick.

### Counterfactual Sandbox *(Claude)*
Take any past decision and re-run it under altered conditions: "What if Claude had been the brain instead of the default?" "What if the SEC filing feed had been enabled?" "What if position sizing had been 2x?" The wake/say/sleep loop already supports an agent making real-time calls; this wraps it in a deterministic replay harness over historical bars and replayed feed items. Output is a `BacktestResult`-shaped artifact that drops into the existing Chart > Backtests tab. The architectural cost is "deterministic feed replay" — which is just timestamped JSON.

---

## Intelligence & Signal Research

### Datasource Hunter
A dedicated dashboard for discovering, evaluating, and continuously backtesting data sources for market-predictive power. The methodology:

1. **Identify keyframes** — large, unambiguous market moves (>2σ intraday, earnings gaps, macro shocks)
2. **Source archaeology** — for each keyframe, pull all news, Discord messages, Twitter/X posts, Reddit threads, SEC filings from the N hours before the move
3. **Predictive scoring** — rank sources by how reliably their signal preceded the move vs. followed it (lead/lag analysis)
4. **Ongoing re-ranking** — as new keyframes accumulate, re-score sources automatically; surfaces which feeds are currently most predictive vs. noisy

The goal: a ranked leaderboard of data sources by their actual demonstrated predictive value, not their marketing claims.

### Source Disagreement Heatmap *(Claude)*
When SEC filings, congressional trades, social chatter, and WH releases all touch the same ticker within a short window, surface the disagreement vector: insiders selling while r/WSB is euphoric, congressional buys against a bearish official narrative. Feed items already normalize to a common shape — this is a backend correlation pass plus a heatmap widget. Sentiment scoring is a small NLP module, or more in Helm's spirit: pipe the day's items to the agent and ask for −1/+1 scores. **Disagreement (not consensus) is the actionable signal** — consensus is already priced.

---

## Learning & Onboarding

### Embedded Glossary & Contextual Training Workflow
A thorough in-app learning system that brings a new trader up to speed in a way that's contextually relevant to whatever they're doing right now. Not a static wiki — the glossary surfaces terms from the current widget or feed in view, and the training workflow adapts to what the user is trying to do (e.g. if they opened the strategy lab, start them on signal construction rather than portfolio theory). Paths feel like side quests, not courses.

### Decision Replay Theater *(Claude)*
The AI Decision Feed already stores rationale cards with confidence, cited signals, and realized P&L follow-through. Wrap that in a scrubbable timeline overlaid on a lightweight chart: at any moment `t`, show exactly what feeds were saying, what the agent saw, what it chose, and what happened over the next N bars. Step through your agent's good and bad calls like reviewing a chess game on Lichess. Almost no new backend — just a `?range=` parameter on the existing decisions endpoint and a join against the bars stream. **Trains your intuition by binge-watching the agent's.**

---

## Architecture & Integration

### Fold Nautilus + OpenBB Directly Into the Repo
Rather than treating Nautilus Trader and OpenBB as external optional dependencies a user has to wire up themselves, vendor or deeply integrate them into the Helm repo. The goal: a new user connects a single Interactive Brokers account at startup and gets a fully integrated system — live data, execution, research, and AI in one place. The experience would be dramatically simpler and the integration far more complete than the current optional-install approach.

---

## The Unifying Thread

Helm has already collected a lot of evidence — decisions, feeds, bars, backtests — in well-shaped JSON. Most of the leverage from here isn't acquiring more data. It's building **lenses** that let a trader see across those collected streams in time, in topology, and in disagreement.
