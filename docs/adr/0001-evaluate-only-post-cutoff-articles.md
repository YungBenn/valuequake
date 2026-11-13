# Evaluate Opus only on Articles published after its training cutoff

Opus 5.5 was trained on news and prices up to its knowledge cutoff, so Predictions on older Articles can draw on outcomes the model already knows. Older history is used only for code-computed values (Base Rates, betas, Analog Outcomes, AMT placebo tests). RPSS and calibration count only Articles published after the cutoff. Batch-scoring history would give a large calibration set right away, but its skill would be inflated. The cost is that labeled Events for calibration come only from live collection, which takes months.
