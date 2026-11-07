---
doc_id: fusion-osagent/changelog
product: fusion-osagent
class: app
entry: developer
doc_type: changelog
title: { zh: "fusion-osagent 变更日志", en: "fusion-osagent changelog" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - CHANGELOG.md
---

# Changelog

All notable changes to fusion-osagent are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Adopt the fusion docs-system v1.1 contract (app x developer): add `docs/product-page.md`, `docs/get-started.md`, `docs/api-reference.md`, `docs/config-reference.md`, `docs/architecture.md`, `docs/troubleshooting.md` with 12-key frontmatter, `CHANGELOG.md`, hardened `ci/validate_docs.py`, and a docs validation GitHub workflow.

### Changed

- Add docs-contract frontmatter to `README.md`, `README_CN.md`, `AGENTS.md`, `CLAUDE.md`, `docs/SLO.md`.

## [1.1.0rc3] - 2026-10-29

### Fixed

- Reassign the `serve-metrics` default port 9100 -> 11571 to avoid the node-exporter collision (issue #6).

## [1.1.0rc2]

### Changed

- Release housekeeping (version bump).

> Earlier release history is tracked in git history and GitHub Releases.
