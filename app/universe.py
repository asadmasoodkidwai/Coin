"""Starter universe: large US companies, roughly by size. Not an official index; edit freely or type your own tickers."""
TOP100 = """AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO JPM LLY V UNH XOM MA COST HD PG JNJ ABBV WMT NFLX BAC CRM ORCL CVX KO MRK AMD PEP
TMO ADBE LIN ACN MCD CSCO ABT WFC IBM GE DIS QCOM INTU TXN CAT VZ AMGN PM ISRG NOW UBER GS BKNG SPGI AXP RTX T LOW MS HON NEE
PFE UNP ETN BLK SYK COP BA PGR TJX C VRTX ADP SCHW LMT MDT BSX PANW DE MU ADI GILD MMC CB SBUX AMT INTC BMY SO MDLZ CME KLAC
LRCX ANET APH CI TMUS MO DUK ICE SHW ZTS WM EQIX CDNS SNPS AON TGT NKE PYPL""".split()


COMPANIES = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMZN": "Amazon", "GOOGL": "Alphabet (Google)", "META": "Meta Platforms",
 "TSLA": "Tesla", "AVGO": "Broadcom", "JPM": "JPMorgan Chase", "LLY": "Eli Lilly", "V": "Visa", "UNH": "UnitedHealth Group", "XOM": "Exxon Mobil",
 "MA": "Mastercard", "COST": "Costco", "HD": "Home Depot", "PG": "Procter & Gamble", "JNJ": "Johnson & Johnson", "ABBV": "AbbVie", "WMT": "Walmart",
 "NFLX": "Netflix", "BAC": "Bank of America", "CRM": "Salesforce", "ORCL": "Oracle", "CVX": "Chevron", "KO": "Coca-Cola", "MRK": "Merck",
 "AMD": "AMD (Advanced Micro Devices)", "PEP": "PepsiCo", "TMO": "Thermo Fisher Scientific", "ADBE": "Adobe", "LIN": "Linde", "ACN": "Accenture",
 "MCD": "McDonald's", "CSCO": "Cisco", "ABT": "Abbott Laboratories", "WFC": "Wells Fargo", "IBM": "IBM", "GE": "GE Aerospace", "DIS": "Walt Disney",
 "QCOM": "Qualcomm", "INTU": "Intuit", "TXN": "Texas Instruments", "CAT": "Caterpillar", "VZ": "Verizon", "AMGN": "Amgen", "PM": "Philip Morris International",
 "ISRG": "Intuitive Surgical", "NOW": "ServiceNow", "UBER": "Uber", "GS": "Goldman Sachs", "BKNG": "Booking Holdings", "SPGI": "S&P Global",
 "AXP": "American Express", "RTX": "RTX", "T": "AT&T", "LOW": "Lowe's", "MS": "Morgan Stanley", "HON": "Honeywell", "NEE": "NextEra Energy", "PFE": "Pfizer",
 "UNP": "Union Pacific", "ETN": "Eaton", "BLK": "BlackRock", "SYK": "Stryker", "COP": "ConocoPhillips", "BA": "Boeing", "PGR": "Progressive",
 "TJX": "TJX Companies", "C": "Citigroup", "VRTX": "Vertex Pharmaceuticals", "ADP": "Automatic Data Processing", "SCHW": "Charles Schwab",
 "LMT": "Lockheed Martin", "MDT": "Medtronic", "BSX": "Boston Scientific", "PANW": "Palo Alto Networks", "DE": "Deere & Company", "MU": "Micron Technology",
 "ADI": "Analog Devices", "GILD": "Gilead Sciences", "MMC": "Marsh McLennan", "CB": "Chubb", "SBUX": "Starbucks", "AMT": "American Tower", "INTC": "Intel",
 "BMY": "Bristol Myers Squibb", "SO": "Southern Company", "MDLZ": "Mondelez International", "CME": "CME Group", "KLAC": "KLA Corporation",
 "LRCX": "Lam Research", "ANET": "Arista Networks", "APH": "Amphenol", "CI": "Cigna", "TMUS": "T-Mobile US", "MO": "Altria", "DUK": "Duke Energy",
 "ICE": "Intercontinental Exchange", "SHW": "Sherwin-Williams", "ZTS": "Zoetis", "WM": "Waste Management", "EQIX": "Equinix", "CDNS": "Cadence Design Systems",
 "SNPS": "Synopsys", "AON": "Aon", "TGT": "Target", "NKE": "Nike", "PYPL": "PayPal", "SPY": "S&P 500 ETF (SPDR)", "QQQ": "Nasdaq-100 ETF (Invesco)"}
