# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- `sync` command for bi-directional sync.
- The `sync` command closes tasks on Todoist if task was closed on TaskWarrior.

### Changed
- `migrate` command will close task on Taskwarrior if it is closed on
  Todoist.
