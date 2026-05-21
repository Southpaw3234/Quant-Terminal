/**
 * Netlify serverless function — Yahoo Finance proxy
 * Fetches chart + summary data server-side (no CORS, no crumb needed from browser)
 * Called by the dashboard as: /.netlify/functions/quote?symbol=AAPL
 */

const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
  'Accept': 'application/json, text/plain, */*',
  'Accept-Language': 'en-US,en;q=0.9',
  'Referer': 'https://finance.yahoo.com/',
  'Origin': 'https://finance.yahoo.com',
};

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Content-Type': 'application/json',
};

exports.handler = async function(event) {
  if (event.httpMethod === 'OPTIONS') {
    return { statusCode: 204, headers: CORS, body: '' };
  }

  const symbol = (event.queryStringParameters?.symbol || '').toUpperCase().trim();
  if (!symbol) {
    return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: 'Missing symbol' }) };
  }

  try {
    // Fetch chart data and summary in parallel
    const [chartRes, summaryRes] = await Promise.allSettled([
      fetch(
        `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=1d&range=1mo`,
        { headers: HEADERS }
      ),
      fetch(
        `https://query1.finance.yahoo.com/v10/finance/quoteSummary/${encodeURIComponent(symbol)}?modules=price,summaryDetail,defaultKeyStatistics`,
        { headers: HEADERS }
      ),
    ]);

    const chart   = chartRes.status   === 'fulfilled' && chartRes.value.ok
                    ? await chartRes.value.json()   : null;
    const summary = summaryRes.status === 'fulfilled' && summaryRes.value.ok
                    ? await summaryRes.value.json() : null;

    if (!chart?.chart?.result?.[0]) {
      return { statusCode: 404, headers: CORS, body: JSON.stringify({ error: 'Ticker not found' }) };
    }

    return {
      statusCode: 200,
      headers: CORS,
      body: JSON.stringify({ chart, summary }),
    };
  } catch (e) {
    return {
      statusCode: 502,
      headers: CORS,
      body: JSON.stringify({ error: e.message }),
    };
  }
};
