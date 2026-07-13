from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common.io_utils import append_jsonl, read_json, read_text, read_yaml, write_json, write_text
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file


def _memory_paths(config_path: str | Path) -> dict[str, Path | int]:
    path = Path(config_path).resolve()
    config = read_yaml(path)
    if not isinstance(config, dict) or not isinstance(config.get("memory"), dict):
        raise ValueError("memory.yaml must define a memory object")
    memory = config["memory"]
    required = ["root_dir", "global_memory_dir", "conversation_memory_dir", "index_path", "max_memory_chars"]
    missing = [name for name in required if name not in memory]
    if missing:
        raise ValueError(f"memory.yaml missing: {', '.join(missing)}")
    root = resolve_from_file(memory["root_dir"], path)
    max_chars = memory["max_memory_chars"]
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_memory_chars must be a positive integer")
    return {
        "root": root,
        "global": root / memory["global_memory_dir"],
        "conversations": root / memory["conversation_memory_dir"],
        "index": root / memory["index_path"],
        "max_chars": max_chars,
    }


def _read_index(index_path: Path) -> dict:
    if not index_path.exists():
        return {}
    index = read_json(index_path)
    if not isinstance(index, dict):
        raise ValueError("memory_index.json must be an object")
    return index


def load_memory(
    config_path: str,
    selected_memory_ids: list[str],
    use_global_memory: bool,
    query: str | None = None,
    outdir: str | None = None,
) -> dict:
    if not isinstance(selected_memory_ids, list) or not all(isinstance(item, str) for item in selected_memory_ids):
        raise ValueError("selected_memory_ids must be a list of strings")
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    ordered_ids = []
    if use_global_memory:
        ordered_ids.extend(sorted(key for key, item in index.items() if item.get("memory_type") == "global"))
    ordered_ids.extend(selected_memory_ids)
    ordered_ids = list(dict.fromkeys(ordered_ids))

    docs = []
    errors = []
    remaining = int(paths["max_chars"])
    any_truncated = False
    for memory_id in ordered_ids:
        metadata = index.get(memory_id)
        if not isinstance(metadata, dict):
            errors.append({"memory_id": memory_id, "type": "MemoryNotFound", "message": "memory_id does not exist"})
            continue
        relative_path = metadata.get("path")
        if not isinstance(relative_path, str):
            errors.append({"memory_id": memory_id, "type": "InvalidMetadata", "message": "memory path is missing"})
            continue
        document_path = (paths["root"] / relative_path).resolve()
        try:
            document_path.relative_to(paths["root"].resolve())
        except ValueError:
            errors.append({"memory_id": memory_id, "type": "InvalidPath", "message": "memory path escapes root"})
            continue
        if not document_path.is_file():
            errors.append({"memory_id": memory_id, "type": "FileNotFoundError", "message": f"memory file not found: {relative_path}"})
            continue
        original = read_text(document_path)
        included = original[:remaining] if remaining > 0 else ""
        truncated = len(included) < len(original)
        any_truncated = any_truncated or truncated
        if included:
            docs.append(
                {
                    "memory_id": memory_id,
                    "memory_type": metadata.get("memory_type"),
                    "title": metadata.get("title", memory_id),
                    "path": relative_path,
                    "content": included,
                    "original_chars": len(original),
                    "included_chars": len(included),
                    "truncated": truncated,
                }
            )
            remaining -= len(included)
    if errors and docs:
        status = "partial"
    elif errors:
        status = "error"
    else:
        status = "success"
    result = {
        "status": status,
        "query": query,
        "selected_memory_docs": docs,
        "max_memory_chars": paths["max_chars"],
        "total_chars": sum(item["included_chars"] for item in docs),
        "truncated": any_truncated,
        "errors": errors,
    }
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "selected_memory.json")
        append_jsonl(
            {
                "timestamp": now_iso(),
                "operation": "load",
                "status": status,
                "selected_ids": [item["memory_id"] for item in docs],
                "errors": errors,
            },
            output_dir / "memory_log.jsonl",
        )
    return result

def _keyword_search_memory(
      index: dict,
      root: Path,
      query: str,
      top_k: int = 5,
      max_chars: int = 2000,
  ) -> list[dict]:
      """Search memory documents by TF-IDF + cosine similarity, return top-k ranked by score."""
      if not index or not query or not query.strip():
          return []

      try:
          import jieba
      except ImportError:
          raise ImportError("jieba is required for keyword search. pip install jieba")
      try:
          from sklearn.feature_extraction.text import TfidfVectorizer
          from sklearn.metrics.pairwise import cosine_similarity
      except ImportError:
          raise ImportError("scikit-learn is required for keyword search. pip install scikit-learn")

      entries = []
      for memory_id, metadata in index.items():
          if not isinstance(metadata, dict):
              continue
          relative_path = metadata.get("path")
          if not isinstance(relative_path, str):
              continue
          document_path = (root / relative_path).resolve()
          try:
              document_path.relative_to(root.resolve())
          except ValueError:
              continue
          if not document_path.is_file():
              continue
          original = read_text(document_path)
          if not original.strip():
              continue
          entries.append({
              "memory_id": memory_id,
              "memory_type": metadata.get("memory_type"),
              "title": metadata.get("title", memory_id),
              "path": relative_path,
              "original": original,
              "original_chars": len(original),
          })

      if not entries:
          return []

      def _tokenize(text):
          return " ".join(jieba.cut(text))

      documents = [_tokenize(e["original"]) for e in entries]
      query_tok = _tokenize(query.strip())

      vectorizer = TfidfVectorizer(token_pattern=r"\S+")
      tfidf_matrix = vectorizer.fit_transform(documents + [query_tok])
      query_vec = tfidf_matrix[-1]
      doc_vecs = tfidf_matrix[:-1]
      similarities = cosine_similarity(query_vec, doc_vecs).flatten()

      ranked = sorted(zip(entries, similarities), key=lambda x: x[1], reverse=True)

      docs = []
      per_doc_chars = max(200, max_chars // max(top_k, 1))
      for entry, score in ranked[:top_k]:
          if score <= 0:
              break
          included = entry["original"][:per_doc_chars]
          truncated = len(included) < entry["original_chars"]
          if included:
              docs.append({
                  "memory_id": entry["memory_id"],
                  "memory_type": entry["memory_type"],
                  "title": entry["title"],
                  "path": entry["path"],
                  "content": included,
                  "original_chars": entry["original_chars"],
                  "included_chars": len(included),
                  "truncated": truncated,
                  "score": round(float(score), 4),
              })
              

      return docs
def _llm_summarize(
      messages: list,
      trace: dict,
      answer: str,
      model_config_path: str,
      max_chars: int = 600,
  ) -> str:
      """Use local Qwen3.5-4B to generate a conversation summary."""
      try:
          import torch
          from transformers import AutoModelForCausalLM, AutoTokenizer
      except ImportError as exc:
          raise RuntimeError("LLM summarization requires transformers and torch") from exc

      config_path = Path(model_config_path).resolve()
      config = read_yaml(config_path)
      model_cfg = config.get("model", {})
      gen_cfg = config.get("generation", {})

      model_str = model_cfg.get("model_name_or_path")
      tokenizer_str = model_cfg.get("tokenizer_name_or_path", model_str)
      if not isinstance(model_str, str):
          raise ValueError("model_name_or_path is required in model.yaml")

      model_path = resolve_from_file(model_str, config_path)
      tokenizer_path = resolve_from_file(tokenizer_str, config_path)

      user_qs = []
      tool_names = []
      for msg in messages:
          if msg.get("role") == "user" and msg.get("content"):
              user_qs.append(msg["content"][:200])
          elif msg.get("role") == "assistant":
              for tc in msg.get("tool_calls", []):
                  tool_names.append(tc.get("name", "unknown"))

      prompt = (
          "请用一段 200 字以内的中文总结以下 Agent 对话，只输出总结文本。\n\n"
          f"用户提问: {'; '.join(user_qs) if user_qs else '无'}\n"
          f"调用工具: {', '.join(dict.fromkeys(tool_names)) if tool_names else '无'}\n"
          f"LLM 调用 {trace.get('llm_call_count', '?')} 次，工具回合 {trace.get('tool_rounds_used', '?')} 次\n"
          f"最终回答: {answer[:500]}\n\n"
          "总结:"
      )

      dtype = torch.bfloat16
      tokenizer = AutoTokenizer.from_pretrained(
          str(tokenizer_path),
          local_files_only=True,
          trust_remote_code=True,
      )
      model = AutoModelForCausalLM.from_pretrained(
          str(model_path),
          local_files_only=True,
          trust_remote_code=True,
          torch_dtype=dtype,
          device_map="auto",
      )

      chat = [{"role": "user", "content": prompt}]
      inputs = tokenizer.apply_chat_template(
          chat, tokenize=True, add_generation_prompt=True,
          return_tensors="pt", return_dict=True,
          enable_thinking=False,
      )
      device = next(model.parameters()).device
      inputs = inputs.to(device)
      input_len = inputs["input_ids"].shape[-1]

      with torch.no_grad():
          generated = model.generate(
              **inputs,
              max_new_tokens=int(gen_cfg.get("max_new_tokens", 256)),
              do_sample=False,
          )

      new_tokens = generated[0][input_len:]
      raw = tokenizer.decode(new_tokens, skip_special_tokens=True)
      import re
      raw = re.sub(r'<\|begin_of_thought\|>.*?<\|end_of_thought\|>', '', raw, flags=re.DOTALL)
      raw = re.sub(r'<\|begin▁of▁thought\|>.*?<\|end▁of▁thought\|>', '', raw, flags=re.DOTALL)
      raw = re.sub(r'^Thinking Process:.*?\n\n', '', raw, flags=re.DOTALL)
      raw = re.sub(r'^思考过程[：:].*?\n\n', '', raw, flags=re.DOTALL)
      summary = raw.strip()
      return summary[:max_chars]      
def _safe_conversation_id(conversation_id: str) -> str:
    if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", conversation_id):
        raise ValueError("conversation_id may only contain letters, numbers, dot, underscore, and hyphen")
    return conversation_id


def save_memory(
      config_path: str,
      conversation_id: str,
      save_type: str,
      messages_path: str,
      trace_path: str,
      answer_path: str,
      outdir: str | None = None,
      model_config: str | None = None,
  ) -> dict:
    conversation_id = _safe_conversation_id(conversation_id)
    if save_type not in {"conversation", "global"}:
        raise ValueError("save_type must be conversation or global")
    paths = _memory_paths(config_path)
    messages = read_json(messages_path)
    trace = read_json(trace_path)
    answer = read_text(answer_path).strip()
    if not isinstance(messages, list) or not isinstance(trace, dict):
        raise ValueError("messages must be an array and trace must be an object")
    now = now_iso()
    memory_id = f"mem_{save_type}_{conversation_id}"
    target_dir = paths["conversations"] if save_type == "conversation" else paths["global"]
    relative_dir = "conversations" if save_type == "conversation" else "global"
    target_path = Path(target_dir) / f"{conversation_id}.md"
    relative_path = f"{relative_dir}/{conversation_id}.md"
    title = f"{save_type.title()} {conversation_id}"
    if model_config is None:
        model_config = str(Path(config_path).resolve().parent / "model.yaml")
    compressed = _llm_summarize(messages, trace, answer, model_config)
    markdown = (
        f"# {title}\n\n"
        f"- memory_id: `{memory_id}`\n"
        f"- conversation_id: `{conversation_id}`\n"
        f"- created_or_updated_at: `{now}`\n\n"
        "## Conversation Summary\n\n"
        f"{compressed}\n\n"
        "## Final Answer\n\n"
        f"{answer}\n\n"
        "## Messages\n\n```json\n"
        f"{json.dumps(messages, ensure_ascii=False, indent=2)}\n```\n\n"
        "## Trace\n\n```json\n"
        f"{json.dumps(trace, ensure_ascii=False, indent=2)}\n```\n"
    )
    write_text(markdown, target_path)
    index = _read_index(paths["index"])
    existing = index.get(memory_id, {})
    created_at = existing.get("created_at", now)
    index[memory_id] = {
        "memory_id": memory_id,
        "memory_type": save_type,
        "title": title,
        "summary": compressed,
        "path": relative_path,
        "conversation_id": conversation_id,
        "created_at": created_at,
        "updated_at": now,
    }
    write_json(index, paths["index"])
    result = {
        "status": "success",
        "memory_id": memory_id,
        "memory_type": save_type,
        "conversation_id": conversation_id,
        "title": title,
        "summary": compressed,
        "path": relative_path,
        "index_path": Path(paths["index"]).name,
        "created_at": created_at,
        "updated_at": now,
        "source_paths": {
            "messages": str(messages_path),
            "trace": str(trace_path),
            "answer": str(answer_path),
        },
    }
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "saved_memory.json")
        append_jsonl(
            {"timestamp": now, "operation": "save", "status": "success", "memory_id": memory_id},
            output_dir / "memory_log.jsonl",
        )
    return result


def parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select or save local memory documents.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--select_memory_ids", nargs="*")
    parser.add_argument("--use_global_memory", type=parse_bool)
    parser.add_argument("--query")
    parser.add_argument("--save_type", choices=["conversation", "global"])
    parser.add_argument("--save_input_path")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--search_mode", choices=["id", "keyword"], default="id")
    parser.add_argument("--top_k", type=int, default=5)
    parser.add_argument("--model_config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = resolve_cli_path(args.config)
        outdir = resolve_cli_path(args.outdir)
        if args.save_type or args.save_input_path:
            if not args.save_type or not args.save_input_path:
                raise ValueError("--save_type and --save_input_path must be provided together")
            input_path = resolve_cli_path(args.save_input_path)
            payload = read_json(input_path)
            if payload.get("save_type") != args.save_type:
                raise ValueError("CLI save_type must match memory_save_input.json")
            base = input_path.parent
            result = save_memory(
                  str(config_path),
                  payload["conversation_id"],
                  args.save_type,
                  str((base / payload["messages_path"]).resolve()),
                  str((base / payload["trace_path"]).resolve()),
                  str((base / payload["answer_path"]).resolve()),
                  str(outdir),
                  model_config=str(resolve_cli_path(args.model_config)) if args.model_config else None,
              )
            print(outdir / "saved_memory.json")
        elif args.search_mode == "keyword":
              if not args.query or not args.query.strip():
                  raise ValueError("keyword search mode requires --query")
              if args.top_k < 1:
                  raise ValueError("--top_k must be a positive integer")
              paths = _memory_paths(config_path)
              index = _read_index(paths["index"])
              docs = _keyword_search_memory(
                  index,
                  paths["root"],
                  args.query,
                  top_k=args.top_k,
                  max_chars=int(paths["max_chars"]),
              )
              result = {
                  "status": "success" if docs else "error",
                  "search_mode": "keyword",
                  "query": args.query,
                  "top_k": args.top_k,
                  "selected_memory_docs": docs,
                  "max_memory_chars": paths["max_chars"],
                  "total_chars": sum(d["included_chars"] for d in docs),
                  "truncated": any(d["truncated"] for d in docs),
                  "errors": [],
              }
              output_dir = Path(outdir)
              write_json(result, output_dir / "selected_memory.json")
              append_jsonl(
                  {
                      "timestamp": now_iso(),
                      "operation": "load",
                      "mode": "keyword",
                      "status": result["status"],
                      "query": args.query,
                      "top_k": args.top_k,
                      "selected_ids": [d["memory_id"] for d in docs],
                  },
                  output_dir / "memory_log.jsonl",
              )
              print(output_dir / "selected_memory.json")    
        else:
            if args.select_memory_ids is None and args.use_global_memory is None:
                raise ValueError("select mode requires --select_memory_ids or --use_global_memory")
            result = load_memory(
                str(config_path),
                args.select_memory_ids or [],
                bool(args.use_global_memory),
                args.query,
                str(outdir),
            )
            print(outdir / "selected_memory.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
