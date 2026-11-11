# kb/ (the user's knowledge base)

**Status: mock.** The user has the real knowledge base elsewhere; its path is a profile value (`knowledge.root`). Until then this folder holds this README and `_example/`, which shows the layout with text files and no images. Images under `kb/` are Git-ignored; never commit them.

## Layout

```text
kb/
  <folder>/                    one folder declares one or more concepts; images may sit in nested folders
    infor.txt                  optional (accepted names are configuration: infor.txt, info.txt)
    <image files>              every image shows all concepts the folder declares
  kb_overrides.yaml            optional, keyed by concept name
```

- **Info file** (decision 9): line 1 holds the concept names, line 2 the descriptions. Both are split by `knowledge.concept_separator` (default `|`) and matched by position.
- **Same name, one concept** (decision 10): a name in several folders is one concept; folder `kimono` and folder `kimono_donut` (info file line 1: `kimono | donut`) both give the concept `kimono`.
- **No info file, or an info file that is unclear** (decisions 30, 38 to 42): a model names the folder. Every info file is read by a model that judges whether it gives usable concept names (the `info_assessor` role, text only); a clear file is used exactly as you wrote it. When the sampled images show unrelated subjects, the sync report flags the folder and nothing splits it. One call of the `folder_namer` role looks at images drawn at random from the folder (`knowledge.naming.sample_size`, default 5; all of them when the folder has fewer; a folder with no image is skipped and reported), with the folder name and any unusable info-file text as hints that may be wrong, and returns the concept name or names and a one-sentence description for each ([K2](../docs/stages/K2-observe.md)). Folder names are used as given and need no special characters; nothing splits them. Such names are used at once, have origin `generated`, are listed in the sync report for you to check, and `kb_overrides.yaml` corrects them and always wins. Examples: folder `Hội An` (no info file, a name that fits its images) and folder `New folder (2)` (a name that says nothing about its content): the model names both from their images.
- A generated description is not a description conflict with another folder's description.
- Example folders in `_example/`: `kimono_donut/` (info file declares two concepts), `Hội An/` (no info file, name fits), `New folder (2)/` (no info file, name says nothing about the content), `kimono/`, `ao_dai/`, and two uniform folders that share a start of their names: they are two separate concepts that a general mention may both fit, which the planner decides from context (decision 31).
- A folder whose parts do not match in count is reported in the sync report and is not indexed until fixed.
- Keep names and descriptions verbatim in UTF-8; descriptions may be in any language.

## Rules for agents

1. **Read only.** Never rename, move, edit or delete anything here (R-014). `kb sync` is the only writer of derived data.
2. Scenario copies and all derived data live under `data_root`, never in place.
3. `_example/` is documentation, not data: exclude it from every run.
4. Images under `kb/` are Git-ignored.
