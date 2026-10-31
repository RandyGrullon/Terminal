// REST calls of the demo account's pending orders and portfolio risk (built on the shared request(): same auth,
// retries and Spanish errors as api.paper.*).
import { request } from '../../api/client'
import type { PaperOrder, PaperOrderRequest, PaperOrderResponse, PaperRisk } from '../../api/types'

export const paperOrdersApi = {
  list: (status: 'pending' | 'all' = 'pending', limit = 50) =>
    request<{ orders: PaperOrder[] }>('GET', '/api/paper/orders', { query: { status, limit } }),
  place: (body: PaperOrderRequest) => request<PaperOrderResponse>('POST', '/api/paper/orders', { body }),
  cancel: (body: { order_id?: number; symbol?: string; all?: boolean }) =>
    request<{ cancelled: PaperOrder[] }>('POST', '/api/paper/orders/cancel', { body }),
  risk: () => request<PaperRisk>('GET', '/api/paper/risk'),
}
