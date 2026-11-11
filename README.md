# UNO Q Internet Radio v3

Internet radio for the Arduino UNO Q (2 GB) in Arduino App Lab, now with
Gemma 3 1B running on the board (Large Language Model - Text brick, llama.cpp).

New in v3
* Ask the radio: "play something relaxing", "turn it down a bit", "put on Wazobia".
  Simple requests are understood instantly by rules; the rest go to Gemma, and its
  answer is checked against the station list before anything happens.
* Tidy song titles: "NOW PLAYING: BURNA BOY - LAST LAST (Clean) | coolfm.ng"
  becomes artist "Burna Boy", title "Last Last". Gemma is only used for text the
  rules can't handle, and its answer must use words from the original text.
* Daily listening summary (Insights tab): Python counts minutes per station,
  time of day and top artists; Gemma writes 2-3 sentences from those facts.
  Written automatically at 21:00, or on demand.

The radio works fully without AI: if the model is missing or still loading,
commands fall back to the rules and summaries to a plain template.

Model: `llamacpp:gemma-3-1b-it-Q4_0` from the App Lab model catalog (722 MB download).

From v2: SQLite station database, web UI on port 7000, ICY song titles.
From v1: sketch, Modulino Buttons and LED matrix (unchanged).

`tools/prompt_check.py` runs the Gemma prompts against any OpenAI-compatible
server (the board's runner, LM Studio or Ollama) so you can check the model's answers.
