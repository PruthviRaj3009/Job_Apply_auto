# modules/embeddings/flattener.py
from typing import Any, Dict, List, Tuple, Union

def flatten_json(
    data: Union[Dict[str, Any], List[Any]],
    parent_key: str = "",
    sep: str = ".",
    exclude_keys: set = None
) -> List[Tuple[str, str]]:
    """Recursively flattens nested JSON and returns a list -> flat_data of (key, value) string pairs."""
    if exclude_keys is None:
        exclude_keys = set()

    flat_data = []

    if isinstance(data, dict):
        for k, v in data.items():
            # Partial match, as config/blacklist.py documents it. An exact
            # `in` test let keys like "password_hash" or "linkedin_password"
            # through, embedding credentials into the vector store.
            if any(ex in k.lower() for ex in exclude_keys):
                continue
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            flat_data.extend(flatten_json(v, new_key, sep=sep, exclude_keys=exclude_keys))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            new_key = f"{parent_key}[{i}]"
            flat_data.extend(flatten_json(v, new_key, sep=sep, exclude_keys=exclude_keys))
    else:
        flat_data.append((parent_key, str(data)))  # Force everything to string

    return flat_data