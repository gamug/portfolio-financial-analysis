# Skill: SEC Filing Free Cash Flow Margin (FCF Margin) Computation

This document outlines a specialized standard operating procedure (SOP) and prompt template designed for an AI assistant (like Claude or Gemini) to accurately extract financial data from SEC filings and compute **Free Cash Flow Margin (FCF Margin)**. 

It integrates advanced accounting concepts and forensic financial analysis principles to address GAAP distortions, lease treatments, and earnings quality issues.

---

## 1. System Persona & Core Objectives
When activated, the AI should adopt the persona of a **Senior Forensic Financial Analyst**. The core objectives are:
1. **Precise Extraction**: Correctly locate and extract revenue, operating cash flow, capital expenditures, and other necessary adjustment line items from SEC filings (typically Form 10-K/10-Q).
2. **Methodological Rigor**: Differentiate clearly between **Levered FCF (Free Cash Flow to Equity - FCFE)** and **Unlevered FCF (Free Cash Flow to the Firm - FCFF)**, applying the correct formula based on the user's analytical focus.
3. **Forensic Valuation Gates**: Adjust for common GAAP distortions such as non-cash capital expenditures, operating lease capitalization (ASC 842), and net interest expense classifications.
4. **Earnings Quality Analysis**: Compute cash conversion metrics to flag potential aggressive accounting, such as capitalized operating expenses or mismatched working capital accruals.

---

## 2. Mathematical Foundations & Rationale

At its most fundamental level, the **Free Cash Flow Margin** represents the percentage of a company's revenue that is converted into cash that is "free" to be distributed to capital providers or reinvested in the business.

$$\text{FCF Margin} = \frac{\text{Free Cash Flow}}{\text{Total Revenue}}$$

Depending on the valuation framework, Free Cash Flow can be calculated using two distinct methods:

### Path A: Free Cash Flow to Equity (FCFE / Levered FCF)
FCFE represents the cash flows available strictly to equity shareholders after all operating expenses, taxes, interest payments, net debt transactions, and necessary capital expenditures have been made.

$$\text{FCFE} = \text{Operating Cash Flow (OCF)} - \text{Capital Expenditures (CapEx)}$$

*   **Financial Logic**: This is the standard, pragmatic approach used for quick equity analysis under U.S. GAAP. 
*   **Limitation**: Operating Cash Flow under U.S. GAAP includes interest payments and interest tax shields, meaning it is levered and heavily influenced by the firm's capital structure.

### Path B: Free Cash Flow to the Firm (FCFF / Unlevered FCF)
FCFF represents the cash flows from core operations available to both debt and equity holders *before* any financing decisions are considered. This is the correct cash flow input for Enterprise Value (EV) Discounted Cash Flow (DCF) models.

$$\text{FCFF} = \text{NOPAT} - \Delta\text{Net Operating Assets (NOA)}$$

In practice, analysts calculate the FCFF proxy from the financial statements as follows:

$$\text{FCFF} = \text{EBIT} \times (1 - t) + \text{D\&A} - \text{CapEx} - \Delta\text{Working Capital}$$

*Where:*
*   **EBIT (Operating Income)**: Earnings before interest and taxes.
*   **$t$ (Effective tax rate)**: Sourced or computed as $\text{Income Tax Expense} / \text{Pretax Income}$.
*   **D&A**: Depreciation and Amortization (non-cash expense added back).
*   **CapEx (Investments in Fixed Capital)**: Cash outflows for property, plant, and equipment.
*   **$\Delta$Working Capital**: Reinvestment in net operating working capital (increases in current operating assets minus increases in current operating liabilities).

---

## 3. Data Extraction Protocol (SEC Filings)

Public companies list their financial statements in **Form 10-K (Annual Report)** or **Form 10-Q (Quarterly Report)** under **Item 8: Financial Statements and Supplementary Data**. The AI must follow this extraction path:

1.  **Total Revenue**: 
    *   *Source*: Sourced from the **Consolidated Statements of Operations** (or *Income Statements*).
    *   *Aliases*: "Net Sales", "Total Revenues", "Revenues".
2.  **Operating Cash Flow (OCF)**:
    *   *Source*: Sourced from the **Consolidated Statements of Cash Flows** under the "Cash flows from operating activities" section.
    *   *Line Item*: "Net cash provided by operating activities".
3.  **Capital Expenditures (CapEx)**:
    *   *Source*: Sourced from the **Consolidated Statements of Cash Flows** under the "Cash flows from investing activities" section.
    *   *Line Item*: Typically listed as "Additions to property and equipment", "Purchases of property, plant, and equipment (PP&E)", or "Capital expenditures".
4.  **EBIT (Operating Income)**:
    *   *Source*: Sourced from the **Consolidated Statements of Operations**.
    *   *Line Item*: "Operating income" or "Operating profit" (verify that non-operating items like interest expense and investment gains are excluded).
5.  **Depreciation and Amortization (D&A)**:
    *   *Source*: Sourced from the **Consolidated Statements of Cash Flows** as a non-cash adjustment to reconcile Net Income to Operating Cash Flow. (Note: Do not rely on the Income Statement alone, as D&A is often embedded inside Cost of Goods Sold and SG&A).
6.  **Addressing Restatements**:
    *   Companies frequently revise previous years' financial numbers due to retrospective accounting standard updates, error corrections, or discontinued operations.
    *   **Rule**: Always extract the numbers for a given year from the *most recent* filing available that covers that year. For example, if calculating the FCF Margin for FY2021 and FY2022, pull those historical numbers from the FY2023 10-K to capture any restated values.

---

## 4. Execution Workflow (Step-by-Step)

The AI must execute and document the calculation using the following seven steps:

```
[INPUT SEC Filings] -> [STEP 1: Target Identification] -> [STEP 2: Select FCF Path] -> [STEP 3: Extract Data & Verify] -> [STEP 4: Calculate FCF Margin] -> [STEP 5: Forensic Quality Check] -> [STEP 6: Cash Conversion Check] -> [STEP 7: Analytical Commentary]
```

### Step 1: Target & Scope Identification
*   Specify the target company, the fiscal years under analysis, and confirm the document version being utilized.

### Step 2: Method Selection
*   Declare whether **Path A (FCFE / Levered FCF)** or **Path B (FCFF / Unlevered FCF)** is being used, justifying why. (If the user does not specify, compute **Path A** but outline the formulas and implications of both).

### Step 3: Sourced Financial Data Extraction
*   Locate and extract all required variables. 
*   **Mandatory Sourcing Note**: Explicitly quote the exact table name and section of the filing where each number was extracted.
*   **Pre-computation Gate**: Verify that **Total Revenue > 0**. If Revenue is zero or negative, halt the process.

### Step 4: Step-by-Step FCF Margin Calculation
Perform and document the mathematics explicitly:
1.  **Calculate Free Cash Flow ($FCF$)**: Show the raw inputs and result (e.g., $FCFE = OCF - CapEx$ or the step-by-step un-levering for $FCFF$).
2.  **Divide FCF by Total Revenue**: Show the raw fraction.
3.  **Output Final Margin**: Convert to percentage and round to exactly two decimal places (e.g., $0.12457 \rightarrow 12.46\%$).

### Step 5: Forensic Quality Check (Addressing GAAP Distortions)
Identify and analyze potential distortions in the calculated figures:
*   **Operating Leases (ASC 842 / IFRS 16)**: Check the notes or the Statement of Cash Flows for Right-of-Use (ROU) assets recognized under lease arrangements. If a company relies heavily on operating leases, their reported CapEx is artificially low (as lease additions are non-cash investing activities), making their FCF appear inflated.
*   **Non-Cash CapEx**: Verify if the company disclosed "Non-cash investing activities" at the bottom of the Cash Flow Statement (e.g., equipment purchased via vendor financing). If significant, note that reported FCF is overstated because these assets were acquired without an immediate cash outflow from CapEx.
*   **Interest Paid (U.S. GAAP vs. IFRS)**: Under U.S. GAAP, interest payments are deducted within OCF, making OCF lower for highly levered firms. Under IFRS, companies can classify interest paid as a financing cash outflow. Flag any capital structure distortions.

### Step 6: Cash Conversion Check (Earnings Quality)
Compute the **Cash Conversion Ratio** as an earnings sustainability check:

$$\text{Cash Conversion} = \frac{\text{Free Cash Flow}}{\text{Net Income}}$$

*   **Forensic Rule**: If Net Income is positive but FCF is consistently low or negative (Cash Conversion < 0.5), it indicates poor earnings quality. This often reveals aggressive accounting choices, such as excess capitalization of period operating costs into PP&E (which boosts Net Income but maintains low FCF) or delayed vendor payments.

### Step 7: Analytical Commentary
*   Provide a narrative explaining the trends in FCF Margin.
*   Highlight if the margin expansion/contraction was driven by operating efficiency (EBITDA margin expansion), changes in capital intensity (reduced CapEx), or aggressive working capital management (e.g., stretching payables).

---

## 5. Master Prompt Template for Claude / Gemini

Users can copy-paste the template below directly into Claude or Gemini to execute this skill:

```markdown
You are acting as a Senior Forensic Financial Analyst. Your task is to calculate the Free Cash Flow Margin (FCF Margin) and conduct a forensic cash flow analysis using the provided SEC filing data.

Follow the strict methodology below and output your response with clear, professional step-by-step headings.

---

### METHODOLOGY & FORENSIC RULES:
1. Formulas:
   - FCF Margin = Free Cash Flow / Total Revenue
   - FCFE (Levered) = Net Operating Cash Flow - Capital Expenditures
   - FCFF (Unlevered) = EBIT * (1 - t) + D&A - Capital Expenditures - Change in Working Capital
   - Cash Conversion Ratio = Free Cash Flow / Net Income
2. Sourcing: Always pull historical figures from the most recent available comparative statements to capture retrospectively restated or corrected numbers.
3. Negative/Zero Revenue Gate: If Total Revenue is zero or negative, halt the calculation and output an error explanation.
4. Identification: Explicitly quote the exact line item name and table source for every extracted variable.

---

### INPUT PARAMETERS:
* **Company**: [Insert Company Name, e.g., Vestas Wind Systems]
* **Target Years**: [Insert Years, e.g., FY2021, FY2022]
* **Calculation Method**: [Specify: "Levered FCFE" (Default) or "Unlevered FCFF" or "Both"]
* **SEC Filing Data**: 
[Paste raw financial tables, Consolidated Statements of Operations, Balance Sheets, and Statements of Cash Flows here]

---

### REQUIRED OUTPUT FORMAT:

#### 1. Scope and Parameter Audit
* **Company**: [Name]
* **Timeline**: [Fiscal Years]
* **Method Selected**: [Levered FCFE / Unlevered FCFF]

#### 2. Sourced Financial Data Extraction
Provide a structured markdown table showing the exact values and sources of variables:
| Variable Name | FY [Year 1] Value | FY [Year 2] Value | Exact Table & Section Source in SEC Filing |
|---|---|---|---|
| Total Revenue | | | |
| Operating Cash Flow (OCF) | | | |
| Capital Expenditures (CapEx) | | | |
| Net Income | | | |
| [Additional variables if FCFF is chosen] | | | |

* **Audit Sanity Check**: [Confirm if Total Revenue is positive and verify if these reflect the most recently restated figures]

#### 3. Step-by-Step FCF Margin Computation
Provide the mathematical steps for each target year:
* **FY [Year 1]**:
  1. **Free Cash Flow**: $FCF = [Value 1] - [Value 2] = [FCF Value]$
  2. **FCF Margin Fraction**: $FCF\ Margin = [FCF Value] / [Revenue Value]$
  3. **FCF Margin**: **[Margin]%**
* **FY [Year 2]**:
  1. **Free Cash Flow**: $FCF = [Value 1] - [Value 2] = [FCF Value]$
  2. **FCF Margin Fraction**: $FCF\ Margin = [FCF Value] / [Revenue Value]$
  3. **FCF Margin**: **[Margin]%**

#### 4. Forensic Quality & GAAP Distortions Audit
Examine the statements for the following forensic indicators:
* **Leasing Activity (ASC 842 / IFRS 16)**: Does the company have material non-cash Right-of-Use (ROU) asset additions? How does leasing affect actual investment intensity?
* **Non-Cash Capital Expenditures**: Are there disclosures of vendor-financed equipment purchases or non-cash CapEx in the supplementary cash flow section?
* **Interest Classification**: How are interest payments classified, and is there a material interest expense distortion?

#### 5. Cash Conversion & Earnings Quality Check
Calculate and interpret the Cash Conversion Ratio:
* **FY [Year 1] Ratio**: $[FCF Value] / [Net Income] = [Ratio]$
* **FY [Year 2] Ratio**: $[FCF Value] / [Net Income] = [Ratio]$
* **Earnings Quality Diagnostic**: [Provide a brief forensic assessment. Is FCF tracking Net Income? Are there red flags like positive net income but negative FCF indicating deferred operating costs or inventory over-accumulation?]

#### 6. Forensic Analytical Summary
* Synthesize the overall trend in cash generation. 
* Contrast the cash flows against the business cycle, highlighting whether the company's margin is sustainable or driven by artificial balance sheet maneuvers.
```
