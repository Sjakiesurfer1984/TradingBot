import os

# Folders and files to ignore
IGNORE_DIRS = {'.git', '__pycache__', 'venv', '.venv', 'env', '.env', '.idea', '.vscode', 'node_modules', 'logs', 'data', 'docs','TradingBot.egg-info', 'Bot.zip', 'tests', 'TradingBot_mapped','codebase.txt','uv.lock','.dependencygraph', 'out'}
ALLOWED_EXTENSIONS = {'.py', '.md', '.json', '.yaml', '.yml', '.ini'}

with open("codebase.txt", "w", encoding="utf-8") as outfile:
    for root, dirs, files in os.walk("."):
        # Modify dirs in-place to skip ignored directories
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        
        for file in files:
            if any(file.endswith(ext) for ext in ALLOWED_EXTENSIONS):
                filepath = os.path.join(root, file)
                if filepath == "./pack_repo.py":
                    continue
                
                outfile.write(f"\n{'='*60}\n")
                outfile.write(f"FILE: {filepath}\n")
                outfile.write(f"{'='*60}\n\n")
                
                try:
                    with open(filepath, "r", encoding="utf-8") as infile:
                        outfile.write(infile.read() + "\n")
                except Exception as e:
                    outfile.write(f"[Error reading file: {e}]\n")

print("Finished! Upload codebase.txt to the AI.")