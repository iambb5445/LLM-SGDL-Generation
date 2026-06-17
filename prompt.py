import pandas as pd

system_message = "" # TODO

oneshot_prompt = ""

history_prompt_addition = ""

skill_prompt_addition = ""

def get_prompt(data: list[tuple[str, pd.DataFrame]], skill_filename: str|None) -> str:
    sgdls = [sgdl for sgdl, eval in data]
    evals = [eval for sgdl, eval in data]
    curr_sgdl, prev_sgdls = sgdls[-1], sgdls[:-1]
    curr_eval, prev_evals = evals[-1], evals[:-1]
    # TODO
    return ""

def process_response(response: str) -> str:
    # TODO extract from response based on the format that is in the prompt 
    sgdl = ""
    return sgdl