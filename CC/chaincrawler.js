flowchart TD
    A[[ChainCrawlr]]
    
    subgraph "Config Layer"
        B[chains.json] --> C[dex_clients]
        D[settings.yaml] --> E[core]
        F[wallet_secrets.json] --> C
    end

    subgraph "DEX Layer"
        C --> G[Uniswap]
        C --> H[Raydium]
        C --> I[Jupiter]
    end

    subgraph "Core Engine"
        E --> J[Token Scanner]
        J -->|New Pairs| K[Anti-Rug]
        K -->|Safe Tokens| L[Sniper]
        L -->|Trades| M[Portfolio Manager]
        M -->|Exit Signals| N[Auto Exit]
    end

    subgraph "Utilities"
        O[Logger] --> J
        O --> L
        P[Helpers] --> K
    end

    subgraph "Interface"
        Q[Dashboard] <--> M
        R[Notifier] <--> N
        S[Signal Payloads] --> R
    end

    A --> Config_Layer
    A --> DEX_Layer
    DEX_Layer --> Core_Engine
    Core_Engine --> Interface
    Utilities -.-> Core_Engine

    style A fill:#2ecc71,stroke:#27ae60
    style Config_Layer fill:#3498db,stroke:#2980b9
    style DEX_Layer fill:#9b59b6,stroke:#8e44ad
    style Core_Engine fill:#e67e22,stroke:#d35400
    style Interface fill:#1abc9c,stroke:#16a085
    style Utilities fill:#95a5a6,stroke:#7f8c8d