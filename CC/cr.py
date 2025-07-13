import os
import shutil # Used for creating/cleaning the dummy directory for demonstration

def is_hash_in_string(line, hash_index):
    """
    Determines if the '#' character at hash_index is likely inside a string literal.
    This function uses a simplified state machine for quotes and does not
    handle all edge cases like escaped quotes (e.g., '\"') or raw strings (r"").
    """
    in_single_quote = False
    in_double_quote = False
    
    # Iterate characters up to the hash_index
    for i in range(hash_index):
        char = line[i]
        
        # Simple check for escaped quotes: if the previous character was a backslash,
        # then the current quote character is escaped and should not toggle the state.
        if i > 0 and line[i-1] == '\\':
            continue

        if char == '"':
            # Toggle double quote state only if not currently inside a single quote
            if not in_single_quote: 
                in_double_quote = not in_double_quote
        elif char == "'":
            # Toggle single quote state only if not currently inside a double quote
            if not in_double_quote: 
                in_single_quote = not in_single_quote
    
    # If either single or double quote state is active, the hash is likely in a string
    return in_single_quote or in_double_quote

def remove_comments_from_python_file(filepath):
    """
    Removes comments (single-line, multi-line, and inline) from a Python file.
    This function uses heuristics to identify comments and might not be 100%
    accurate for all edge cases involving complex string literals or
    syntactically unusual code.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        cleaned_lines = []
        in_multiline_string = False # Tracks if we are inside a """ or ''' block

        for line in lines:
            stripped_line = line.strip()

            triple_double_quotes = '"""'
            triple_single_quotes = "'''"

            # Check if the line contains triple quotes that might indicate a multiline string/docstring
            has_triple_double = triple_double_quotes in line
            has_triple_single = triple_single_quotes in line

            # Determine if the line is primarily a multiline string delimiter (start or end)
            is_delimiter_line = False
            if has_triple_double and (line.strip().startswith(triple_double_quotes) or line.strip().endswith(triple_double_quotes)):
                is_delimiter_line = True
            if has_triple_single and (line.strip().startswith(triple_single_quotes) or line.strip().endswith(triple_single_quotes)):
                is_delimiter_line = True

            if is_delimiter_line:
                if not in_multiline_string:
                    # If it's a single-line docstring (e.g., '"""Docstring"""')
                    # Check if the line starts and ends with the same triple quote and has an even count
                    if (stripped_line.startswith(triple_double_quotes) and stripped_line.endswith(triple_double_quotes) and stripped_line.count(triple_double_quotes) % 2 == 0) or \
                       (stripped_line.startswith(triple_single_quotes) and stripped_line.endswith(triple_single_quotes) and stripped_line.count(triple_single_quotes) % 2 == 0):
                        # This line is a full single-line docstring, so we skip it
                        continue
                    else:
                        # It's the beginning of a multi-line string/docstring block
                        in_multiline_string = True
                        continue # Skip this line as it's a delimiter
                else:
                    # It's the end of a multi-line string/docstring block
                    in_multiline_string = False
                    continue # Skip this line as it's a delimiter

            # If we are currently inside a multi-line string/docstring block, skip the current line
            # as it's part of the comment/string content
            if in_multiline_string:
                continue

            # Handle single-line comments and inline comments
            hash_index = line.find('#')

            if hash_index != -1:
                # Check if '#' is inside a string literal using the helper function
                if is_hash_in_string(line, hash_index):
                    # If '#' is likely inside a string, keep the whole line as is
                    cleaned_lines.append(line)
                else:
                    # If '#' is a comment, remove the comment part (from '#' to the end of the line)
                    code_part = line[:hash_index].rstrip() # Get the part before the hash and strip trailing whitespace
                    if code_part: # Only add the line if there's actual code left after removing the comment
                        cleaned_lines.append(code_part + '\n')
            else:
                # No '#' found on the line. Keep the line if it contains non-whitespace characters,
                # or if it was an empty line (to preserve original line spacing).
                if stripped_line:
                    cleaned_lines.append(line)
                elif line.endswith('\n'): # Preserve original empty lines
                    cleaned_lines.append('\n')

        # Write the cleaned content back to the original file
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(cleaned_lines)
        print(f"Successfully cleaned: {filepath}")

    except Exception as e:
        print(f"Error processing {filepath}: {e}")

def clean_python_files_in_directory(root_dir):
    """
    Traverses the given directory and removes comments from all Python files.
    The 'logs' directory is explicitly excluded from processing.
    """
    print(f"Starting comment removal from Python files in: {root_dir}")
    if not os.path.isdir(root_dir):
        print(f"Error: Directory '{root_dir}' not found. Please ensure the 'ChainCrawlr' folder exists.")
        return

    # os.walk generates the file names in a directory tree by walking the tree
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Prevent os.walk from entering the 'logs' directory by modifying dirnames in place.
        # This ensures files within 'logs' are not even considered.
        if 'logs' in dirnames:
            dirnames.remove('logs')

        for filename in filenames:
            # Process only Python files
            if filename.endswith('.py'):
                filepath = os.path.join(dirpath, filename)
                print(f"Processing file: {filepath}")
                remove_comments_from_python_file(filepath)
    print("Comment removal process completed for Python files.")

# --- Setup for demonstration/testing in a sandboxed environment ---
# This section creates a dummy 'ChainCrawlr' directory structure with sample
# Python files containing various types of comments. This allows you to run
# the script and see its effects directly.

def create_dummy_structure(base_path="ChainCrawlr"):
    """
    Creates a dummy directory structure mirroring the ChainCrawlr project
    with sample Python files containing comments for testing.
    """
    print(f"Creating dummy directory structure at {base_path}...")
    # Define the structure and some dummy content with comments
    structure = {
        "main.py": """
# Main bot controller and orchestrator
import os # Import os module
def run_bot():
    print("Bot is running") # Log start
    # TODO: Implement bot logic here
    pass
""",
        "core": {
            "token_scanner.py": """
\"\"\"
token_scanner.py
Detects new tokens & filters by safety.
This is a multi-line docstring.
\"\"\"
class TokenScanner:
    def __init__(self):
        self.blacklist = ["scam_token"] # List of known scam tokens
        self.message = "Found new token! #important" # Message with hash inside string
        self.another_string = 'This is a test with a #hash inside single quotes' # Single quote hash
        self.escaped_quote = "This string has a \\" quote and # a comment" # Escaped quote and comment
        self.raw_string = r"C:\\path\\to\\file#notacomment" # Raw string with hash
        self.complex_string = '''
        This is a
        multi-line string
        with a #hash
        inside.
        ''' # Multi-line string with hash
    
    def scan(self, token_address):
        # Check token safety
        if token_address in self.blacklist:
            return False # It's a scam
        return True # Looks good
""",
            "sniper.py": """
# Sniper module
# Executes snipe buys with fast confirmation
def execute_snipe(token, amount):
    \"\"\"Executes a snipe buy for a given token.\"\"\"
    # Simulate a fast transaction
    print(f"Sniping {amount} of {token}") # Debug print
    # Add transaction logic here
    pass # Placeholder
""",
            "anti_rug.py": """
# Anti-rug module
# Validates smart contracts for rugs
def check_rug_potential(contract_address):
    # This function checks for common rug pull patterns.
    # For example, ownership renouncement, liquidity lock status.
    # It's a complex check.
    return False # Assume no rug for now
""",
            "portfolio_manager.py": """
# Manages token balances & valuation
class PortfolioManager:
    def __init__(self):
        self.balances = {} # Stores token balances
    
    def update_balance(self, token, amount):
        self.balances[token] = self.balances.get(token, 0) + amount
        # Ensure the balance is positive
""",
            "auto_exit.py": """
# Handles laddered exits & rug detection
def setup_auto_exit(token, profit_targets):
    # Set up sell orders at different profit levels
    # This function is critical for profit taking.
    print(f"Setting up auto-exit for {token}")
"""
        },
        "dex_clients": {
            "uniswap.py": "# Uniswap client",
            "raydium.py": "# Raydium client",
            "jupiter.py": "# Jupiter client"
        },
        "config": {
            "settings.yaml": "key: value # Inline comment in YAML", # This file won't be processed by .py filter
            "chains.json": "{ \"ethereum\": { \"rpc\": \"...\" } }" # This file won't be processed by .py filter
        },
        "interface": {
            "dashboard.py": """
# Dashboard module
# Real-time dashboard (Streamlit or Flask)
def render_dashboard():
    print("Rendering dashboard")
""",
            "notifier.py": """
# Notifier module
# Sends alerts via Telegram / Discord
def send_alert(message):
    print(f"Sending alert: {message}")
""",
            "signal_payloads.py": """
# Standardized message formats for alerts
ALERT_TYPES = {
    "NEW_TOKEN": "New token detected: {token}", # Format string
    "SNIPE_SUCCESS": "Snipe successful for {token}", # Another format
}
"""
        },
        "utils": {
            "logger.py": """
# Logger utility
# Rotating, structured logger + optional alert hooks
import logging
def setup_logger():
    logging.basicConfig(level=logging.INFO) # Basic setup
    # More advanced logging configuration here
""",
            "helpers.py": """
# Shared utility functions (timing, formatting, gas calc, etc.)
def calculate_gas_fee(base_fee, priority_fee, gas_limit):
    \"\"\"Calculates the total gas fee for a transaction.\"\"\"
    # Formula: (base_fee + priority_fee) * gas_limit
    return (base_fee + priority_fee) * gas_limit # Return value
"""
        },
        "keys": {
            "wallet_secrets.json": "{}" # Dummy empty file
        },
        "logs": {
            "chaincrawler.log": "This is a log file. # This line should not be processed."
        }
    }

    # Helper function to recursively create directories and files
    def create_path(current_path, content):
        if isinstance(content, dict):
            os.makedirs(current_path, exist_ok=True)
            for name, sub_content in content.items():
                create_path(os.path.join(current_path, name), sub_content)
        else:
            # Ensure content is written with UTF-8 encoding
            with open(current_path, 'w', encoding='utf-8') as f:
                f.write(content.strip()) # .strip() to remove leading/trailing newlines from content strings
            print(f"Created: {current_path}")

    create_path(base_path, structure)
    print("Dummy structure created.")

# --- Main execution block ---
if __name__ == "__main__":
    # Define the root directory of your bot project
    bot_root_dir = "ChainCrawlr"

    # Clean up any previously created dummy directory to ensure a fresh test
    if os.path.exists(bot_root_dir):
        print(f"Removing existing '{bot_root_dir}' directory...")
        shutil.rmtree(bot_root_dir)
        print("Removed.")

    # Create the dummy directory structure with sample files for demonstration
    create_dummy_structure(bot_root_dir)

    # Run the main function to remove comments from all Python files
    clean_python_files_in_directory(bot_root_dir)

    print("\n--- Verification ---")
    print(f"The script has finished processing. You can now inspect the '{bot_root_dir}' directory")
    print("to see the changes in the Python files. Comments should have been removed.")
    print("For example, open 'ChainCrawlr/core/token_scanner.py' to verify the changes.")
