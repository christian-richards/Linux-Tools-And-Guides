#!/usr/bin/env bash

set -euo pipefail

# ==========================================
# CONFIGURATION
# ==========================================

# Target destination folder (created in the same directory as the script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAT_DIR="${SCRIPT_DIR}/flat"
MANIFEST="${FLAT_DIR}/path_manifest.txt"

# File Extensions Filter (Case-insensitive)
# Set USE_EXTENSION_FILTER to true to enable filtering
USE_EXTENSION_FILTER=false
# Whitelist mode: ONLY allow these extensions. Blacklist mode: ALLOW ALL EXCEPT these extensions.
EXTENSION_MODE="whitelist" # "whitelist" or "blacklist"
EXTENSIONS=("txt" "png" "jpg" "md")

# Folder Filters (Matched against relative folder paths)
# Whitelist mode: ONLY copy from these folders. Blacklist mode: SKIP these folders.
USE_FOLDER_FILTER=true
FOLDER_MODE="blacklist" # "whitelist" or "blacklist"
FOLDERS=("node_modules" ".git" "temp" "_vendor")

# ==========================================
# HELPER FUNCTIONS
# ==========================================

should_process_file() {
  local rel_path="$1"
  local filename
  filename=$(basename "$rel_path")
  local ext="${filename##*.}"
  [ "$ext" = "$filename" ] && ext="" # Handle files without extension

  # Check folder filters
  if [ "$USE_FOLDER_FILTER" = true ]; then
    local folder_match=false
    for f in "${FOLDERS[@]}"; do
      if [[ "$rel_path" == *"/$f/"* ]] || [[ "$rel_path" == "$f/"* ]]; then
        folder_match=true
        break
      fi
    done

    if [ "$FOLDER_MODE" = "blacklist" ] && [ "$folder_match" = true ]; then
      return 1
    elif [ "$FOLDER_MODE" = "whitelist" ] && [ "$folder_match" = false ]; then
      return 1
    fi
  fi

  # Check extension filters
  if [ "$USE_EXTENSION_FILTER" = true ]; then
    local ext_match=false
    for e in "${EXTENSIONS[@]}"; do
      if [ "${ext,,}" = "${e,,}" ]; then
        ext_match=true
        break
      fi
    done

    if [ "$EXTENSION_MODE" = "blacklist" ] && [ "$ext_match" = true ]; then
      return 1
    elif [ "$EXTENSION_MODE" = "whitelist" ] && [ "$ext_match" = false ]; then
      return 1
    fi
  fi

  return 0
}

# Handles filename collisions in the flat directory (e.g., two files named index.html)
get_unique_flat_name() {
  local orig_path="$1"
  local base_name
  base_name=$(basename "$orig_path")

  if [ ! -e "${FLAT_DIR}/${base_name}" ]; then
    echo "$base_name"
    return
  fi

  # Hash relative path to prevent collisions while keeping original extension
  local hash
  hash=$(echo -n "$orig_path" | md5sum | cut -c1-8)
  local ext="${base_name##*.}"
  local name="${base_name%.*}"

  if [ "$ext" != "$base_name" ]; then
    echo "${name}_${hash}.${ext}"
  else
    echo "${base_name}_${hash}"
  fi
}

# ==========================================
# CORE ACTIONS
# ==========================================

flatten() {
  local source_dir="${1:-}"

  if [ -z "$source_dir" ] || [ ! -d "$source_dir" ]; then
    echo "Error: Please specify a valid source directory to flatten."
    echo "Usage: $0 flatten <source_directory>"
    exit 1
  fi

  # Clean or create flat directory
  mkdir -p "$FLAT_DIR"
  > "$MANIFEST"

  echo "Flattening files from '$source_dir' to '$FLAT_DIR'..."

  # Normalize path
  source_dir=$(cd "$source_dir" && pwd)

  find "$source_dir" -type f | while read -r abs_path; do
    # Get relative path for filter matching
    local rel_path="${abs_path#$source_dir/}"

    if should_process_file "$rel_path"; then
      local flat_filename
      flat_filename=$(get_unique_flat_name "$rel_path")
      local flat_filepath="${FLAT_DIR}/${flat_filename}"

      # Copy file
      cp "$abs_path" "$flat_filepath"

      # Log to manifest: FLAT_NAME|ORIGINAL_FULL_PATH
      echo "${flat_filename}|${abs_path}" >> "$MANIFEST"
      echo "Copied: $rel_path -> $flat_filename"
    fi
  done

  echo "Done. Manifest created at $MANIFEST"
}

restore() {
  if [ ! -f "$MANIFEST" ]; then
    echo "Error: Manifest file not found at $MANIFEST. Cannot restore."
    exit 1
  fi

  echo "Restoring modified files from '$FLAT_DIR' to original locations..."

  while IFS='|' read -r flat_filename orig_path; do
    local flat_filepath="${FLAT_DIR}/${flat_filename}"

    if [ -f "$flat_filepath" ]; then
      # Ensure target directory exists before copying back
      mkdir -p "$(dirname "$orig_path")"
      cp "$flat_filepath" "$orig_path"
      echo "Restored: $flat_filename -> $orig_path"
    else
      echo "Warning: Skipped missing flat file: $flat_filename"
    fi
  done < "$MANIFEST"

  echo "Restore complete."
}

# ==========================================
# COMMAND PARSER
# ==========================================

case "${1:-}" in
  flatten)
    flatten "${2:-}"
    ;;
  restore)
    restore
    ;;
  *)
    echo "Usage:"
    echo "  $0 flatten <directory_path>  - Flatten files into ./flat"
    echo "  $0 restore                  - Restore modified files from ./flat to original paths"
    exit 1
    ;;
esac
