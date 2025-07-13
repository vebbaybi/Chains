import * as fs from 'fs';
import * as path from 'path';

function isHashInString(line: string, hashIndex: number): boolean {
    /**
     * Determines if the '#' character at hashIndex is likely inside a string literal.
     * This function uses a simplified state machine for quotes and does not
     * handle all edge cases like escaped quotes (e.g., '\"') or raw strings (r"").
     */
    let inSingleQuote = false;
    let inDoubleQuote = false;
    
    // Iterate characters up to the hashIndex
    for (let i = 0; i < hashIndex; i++) {
        const char = line[i];
        
        // Simple check for escaped quotes: if the previous character was a backslash,
        // then the current quote character is escaped and should not toggle the state.
        if (i > 0 && line[i-1] === '\\') {
            continue;
        }

        if (char === '"') {
            // Toggle double quote state only if not currently inside a single quote
            if (!inSingleQuote) { 
                inDoubleQuote = !inDoubleQuote;
            }
        } else if (char === "'") {
            // Toggle single quote state only if not currently inside a double quote
            if (!inDoubleQuote) { 
                inSingleQuote = !inSingleQuote;
            }
        }
    }
    
    // If either single or double quote state is active, the hash is likely in a string
    return inSingleQuote || inDoubleQuote;
}

async function removeCommentsFromPythonFile(filepath: string): Promise<void> {
    /**
     * Removes comments (single-line, multi-line, and inline) from a Python file.
     * This function uses heuristics to identify comments and might not be 100%
     * accurate for all edge cases involving complex string literals or
     * syntactically unusual code.
     */
    try {
        const content = await fs.promises.readFile(filepath, 'utf-8');
        const lines = content.split('\n');

        const cleanedLines: string[] = [];
        let inMultilineString = false; // Tracks if we are inside a """ or ''' block

        for (const line of lines) {
            const strippedLine = line.trim();

            const tripleDoubleQuotes = '"""';
            const tripleSingleQuotes = "'''";

            // Check if the line contains triple quotes that might indicate a multiline string/docstring
            const hasTripleDouble = line.includes(tripleDoubleQuotes);
            const hasTripleSingle = line.includes(tripleSingleQuotes);

            // Determine if the line is primarily a multiline string delimiter (start or end)
            let isDelimiterLine = false;
            if (hasTripleDouble && (strippedLine.startsWith(tripleDoubleQuotes) || strippedLine.endsWith(tripleDoubleQuotes))) {
                isDelimiterLine = true;
            }
            if (hasTripleSingle && (strippedLine.startsWith(tripleSingleQuotes) || strippedLine.endsWith(tripleSingleQuotes))) {
                isDelimiterLine = true;
            }

            if (isDelimiterLine) {
                if (!inMultilineString) {
                    // If it's a single-line docstring (e.g., '"""Docstring"""')
                    // Check if the line starts and ends with the same triple quote and has an even count
                    if ((strippedLine.startsWith(tripleDoubleQuotes) && strippedLine.endsWith(tripleDoubleQuotes) && (strippedLine.split(tripleDoubleQuotes).length - 1) % 2 === 0) ||
                       (strippedLine.startsWith(tripleSingleQuotes) && strippedLine.endsWith(tripleSingleQuotes) && (strippedLine.split(tripleSingleQuotes).length - 1) % 2 === 0)) {
                        // This line is a full single-line docstring, so we skip it
                        continue;
                    } else {
                        // It's the beginning of a multi-line string/docstring block
                        inMultilineString = true;
                        continue; // Skip this line as it's a delimiter
                    }
                } else {
                    // It's the end of a multi-line string/docstring block
                    inMultilineString = false;
                    continue; // Skip this line as it's a delimiter
                }
            }

            // If we are currently inside a multi-line string/docstring block, skip the current line
            // as it's part of the comment/string content
            if (inMultilineString) {
                continue;
            }

            // Handle single-line comments and inline comments
            const hashIndex = line.indexOf('#');

            if (hashIndex !== -1) {
                // Check if '#' is inside a string literal using the helper function
                if (isHashInString(line, hashIndex)) {
                    // If '#' is likely inside a string, keep the whole line as is
                    cleanedLines.push(line);
                } else {
                    // If '#' is a comment, remove the comment part (from '#' to the end of the line)
                    const codePart = line.substring(0, hashIndex).trimEnd(); // Get the part before the hash and strip trailing whitespace
                    if (codePart) { // Only add the line if there's actual code left after removing the comment
                        cleanedLines.push(codePart);
                    }
                }
            } else {
                // No '#' found on the line. Keep the line if it contains non-whitespace characters,
                // or if it was an empty line (to preserve original line spacing).
                if (strippedLine) {
                    cleanedLines.push(line);
                } else if (line.endsWith('\n')) { // Preserve original empty lines
                    cleanedLines.push('');
                }
            }
        }

        // Write the cleaned content back to the original file
        await fs.promises.writeFile(filepath, cleanedLines.join('\n'), 'utf-8');
        console.log(`Successfully cleaned: ${filepath}`);

    } catch (e) {
        console.error(`Error processing ${filepath}: ${e}`);
    }
}

async function cleanPythonFilesInDirectory(rootDir: string): Promise<void> {
    /**
     * Traverses the given directory and removes comments from all Python files.
     * The 'logs' directory is explicitly excluded from processing.
     */
    console.log(`Starting comment removal from Python files in: ${rootDir}`);
    if (!fs.existsSync(rootDir) || !fs.statSync(rootDir).isDirectory()) {
        console.error(`Error: Directory '${rootDir}' not found. Please ensure the 'ChainCrawlr' folder exists.`);
        return;
    }

    // Recursive directory walker
    const walkDir = async (dir: string) => {
        const entries = await fs.promises.readdir(dir, { withFileTypes: true });
        
        for (const entry of entries) {
            const fullPath = path.join(dir, entry.name);
            
            if (entry.isDirectory()) {
                if (entry.name === 'logs') {
                    continue; // Skip the logs directory
                }
                await walkDir(fullPath);
            } else if (entry.isFile() && entry.name.endsWith('.py')) {
                console.log(`Processing file: ${fullPath}`);
                await removeCommentsFromPythonFile(fullPath);
            }
        }
    };

    await walkDir(rootDir);
    console.log("Comment removal process completed for Python files.");
}

// --- Setup for demonstration/testing in a sandboxed environment ---
// This section creates a dummy 'ChainCrawlr' directory structure with sample
// Python files containing various types of comments. This allows you to run
// the script and see its effects directly.

interface DirectoryStructure {
    [key: string]: string | DirectoryStructure;
}

async function createDummyStructure(basePath = "ChainCrawlr"): Promise<void> {
    /**
     * Creates a dummy directory structure mirroring the ChainCrawlr project
     * with sample Python files containing comments for testing.
     */
    console.log(`Creating dummy directory structure at ${basePath}...`);
    // Define the structure and some dummy content with comments
    const structure: DirectoryStructure = {
        "main.py": `
# Main bot controller and orchestrator
import os # Import os module
def run_bot():
    print("Bot is running") # Log start
    # TODO: Implement bot logic here
    pass
`,
        "core": {
            "token_scanner.py": `
"""
token_scanner.py
Detects new tokens & filters by safety.
This is a multi-line docstring.
"""
class TokenScanner:
    def __init__(self):
        self.blacklist = ["scam_token"] # List of known scam tokens
        self.message = "Found new token! #important" # Message with hash inside string
        self.another_string = 'This is a test with a #hash inside single quotes' # Single quote hash
        self.escaped_quote = "This string has a \\" quote and # a comment" # Escaped quote and comment
        self.raw_string = r"C:\\\\path\\\\to\\\\file#notacomment" # Raw string with hash
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
`,
            "sniper.py": `
# Sniper module
# Executes snipe buys with fast confirmation
def execute_snipe(token, amount):
    """Executes a snipe buy for a given token."""
    # Simulate a fast transaction
    print(f"Sniping {amount} of {token}") # Debug print
    # Add transaction logic here
    pass # Placeholder
`,
            "anti_rug.py": `
# Anti-rug module
# Validates smart contracts for rugs
def check_rug_potential(contract_address):
    # This function checks for common rug pull patterns.
    # For example, ownership renouncement, liquidity lock status.
    # It's a complex check.
    return False # Assume no rug for now
`,
            "portfolio_manager.py": `
# Manages token balances & valuation
class PortfolioManager:
    def __init__(self):
        self.balances = {} # Stores token balances
    
    def update_balance(self, token, amount):
        self.balances[token] = self.balances.get(token, 0) + amount
        # Ensure the balance is positive
`,
            "auto_exit.py": `
# Handles laddered exits & rug detection
def setup_auto_exit(token, profit_targets):
    # Set up sell orders at different profit levels
    # This function is critical for profit taking.
    print(f"Setting up auto-exit for {token}")
`
        },
        "dex_clients": {
            "uniswap.py": "# Uniswap client",
            "raydium.py": "# Raydium client",
            "jupiter.py": "# Jupiter client"
        },
        "config": {
            "settings.yaml": "key: value # Inline comment in YAML", // This file won't be processed by .py filter
            "chains.json": "{ \"ethereum\": { \"rpc\": \"...\" } }" // This file won't be processed by .py filter
        },
        "interface": {
            "dashboard.py": `
# Dashboard module
# Real-time dashboard (Streamlit or Flask)
def render_dashboard():
    print("Rendering dashboard")
`,
            "notifier.py": `
# Notifier module
# Sends alerts via Telegram / Discord
def send_alert(message):
    print(f"Sending alert: {message}")
`,
            "signal_payloads.py": `
# Standardized message formats for alerts
ALERT_TYPES = {
    "NEW_TOKEN": "New token detected: {token}", # Format string
    "SNIPE_SUCCESS": "Snipe successful for {token}", # Another format
}
`
        },
        "utils": {
            "logger.py": `
# Logger utility
# Rotating, structured logger + optional alert hooks
import logging
def setup_logger():
    logging.basicConfig(level=logging.INFO) # Basic setup
    # More advanced logging configuration here
`,
            "helpers.py": `
# Shared utility functions (timing, formatting, gas calc, etc.)
def calculate_gas_fee(base_fee, priority_fee, gas_limit):
    """Calculates the total gas fee for a transaction."""
    # Formula: (base_fee + priority_fee) * gas_limit
    return (base_fee + priority_fee) * gas_limit # Return value
`
        },
        "keys": {
            "wallet_secrets.json": "{}" // Dummy empty file
        },
        "logs": {
            "chaincrawler.log": "This is a log file. # This line should not be processed."
        }
    };

    // Helper function to recursively create directories and files
    const createPath = async (currentPath: string, content: string | DirectoryStructure) => {
        if (typeof content !== 'string') {
            await fs.promises.mkdir(currentPath, { recursive: true });
            for (const [name, subContent] of Object.entries(content)) {
                await createPath(path.join(currentPath, name), subContent);
            }
        } else {
            // Ensure content is written with UTF-8 encoding
            await fs.promises.writeFile(currentPath, content.trim(), 'utf-8'); // .trim() to remove leading/trailing newlines from content strings
            console.log(`Created: ${currentPath}`);
        }
    };

    await createPath(basePath, structure);
    console.log("Dummy structure created.");
}

// --- Main execution block ---
async function main() {
    // Define the root directory of your bot project
    const botRootDir = "ChainCrawlr";

    // Clean up any previously created dummy directory to ensure a fresh test
    if (fs.existsSync(botRootDir)) {
        console.log(`Removing existing '${botRootDir}' directory...`);
        await fs.promises.rm(botRootDir, { recursive: true, force: true });
        console.log("Removed.");
    }

    // Create the dummy directory structure with sample files for demonstration
    await createDummyStructure(botRootDir);

    // Run the main function to remove comments from all Python files
    await cleanPythonFilesInDirectory(botRootDir);

    console.log("\n--- Verification ---");
    console.log(`The script has finished processing. You can now inspect the '${botRootDir}' directory`);
    console.log("to see the changes in the Python files. Comments should have been removed.");
    console.log("For example, open 'ChainCrawlr/core/token_scanner.py' to verify the changes.");
}

main().catch(err => {
    console.error('An error occurred:', err);
    process.exit(1);
});