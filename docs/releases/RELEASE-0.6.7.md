# BURNRATE 0.6.7

- **Customize which usage limit shows in the floating corner pill.** Pick any connected provider's specific usage limit (e.g. `Claude Code · Primary (Fable weekly)` or `Codex (Weekly)`) or stay on **Auto (most urgent)** directly from the inline **Pill limit** picker in the hover card or from **Settings → Floating pill limit**.
- **Show Claude Fable weekly limits for all Claude accounts.** Reads the `weekly_scoped` Fable usage bucket from every Claude profile's cached plan usage and shows it as **Fable weekly** across Limits, pace forecasts, and the pill limit picker.
- **Clean up hover card scrollbars and footer clipping.** Eliminates horizontal and bright Win32 native scrollbars in the expanded "other providers" list, keeps rows un-squished with a slim theme-matched 5px scrollbar, and keeps `Open full BURNRATE ↗` and `Refresh now` cleanly visible at the bottom.
