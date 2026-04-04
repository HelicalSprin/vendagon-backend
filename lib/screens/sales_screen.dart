import 'package:flutter/material.dart';
import '../services/api_service.dart';

class SalesScreen extends StatefulWidget {
  final ApiService api;
  const SalesScreen({super.key, required this.api});
  @override
  State<SalesScreen> createState() => _SalesScreenState();
}

class _SalesScreenState extends State<SalesScreen> with SingleTickerProviderStateMixin {
  late TabController _tabController;
  Map<String, dynamic>? _data;
  bool _loading = false;
  String? _error;

  // Date range — default today
  late DateTime _startDate;
  late DateTime _endDate;
  String _rangeLabel = 'Today';

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 3, vsync: this);
    _setRange('Today');
  }

  @override
  void dispose() { _tabController.dispose(); super.dispose(); }

  void _setRange(String label) {
    final now = DateTime.now();
    setState(() {
      _rangeLabel = label;
      switch (label) {
        case 'Today':
          _startDate = DateTime(now.year, now.month, now.day);
          _endDate = DateTime(now.year, now.month, now.day, 23, 59, 59);
          break;
        case 'Yesterday':
          final y = now.subtract(const Duration(days: 1));
          _startDate = DateTime(y.year, y.month, y.day);
          _endDate = DateTime(y.year, y.month, y.day, 23, 59, 59);
          break;
        case 'This Week':
          _startDate = now.subtract(Duration(days: now.weekday - 1));
          _startDate = DateTime(_startDate.year, _startDate.month, _startDate.day);
          _endDate = DateTime(now.year, now.month, now.day, 23, 59, 59);
          break;
        case 'This Month':
          _startDate = DateTime(now.year, now.month, 1);
          _endDate = DateTime(now.year, now.month, now.day, 23, 59, 59);
          break;
      }
    });
    _load();
  }

  Future<void> _pickCustomRange() async {
    final picked = await showDateRangePicker(
      context: context,
      firstDate: DateTime(2024),
      lastDate: DateTime.now(),
      initialDateRange: DateTimeRange(start: _startDate, end: _endDate),
      builder: (ctx, child) => Theme(
        data: Theme.of(ctx).copyWith(colorScheme: const ColorScheme.light(primary: Color(0xFF0077FF))),
        child: child!,
      ),
    );
    if (picked != null) {
      setState(() {
        _startDate = picked.start;
        _endDate = DateTime(picked.end.year, picked.end.month, picked.end.day, 23, 59, 59);
        _rangeLabel = 'Custom';
      });
      _load();
    }
  }

  Future<void> _load() async {
    setState(() { _loading = true; _error = null; });
    try {
      final data = await widget.api.getSalesSummary(
        startDate: _startDate.millisecondsSinceEpoch,
        endDate: _endDate.millisecondsSinceEpoch,
      );
      setState(() => _data = data);
    } catch (e) {
      setState(() => _error = e.toString().replaceAll('Exception: ', ''));
    } finally {
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        // Date range selector
        Container(
          color: Colors.white,
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Row(
                  children: ['Today', 'Yesterday', 'This Week', 'This Month'].map((label) {
                    final selected = _rangeLabel == label;
                    return Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        label: Text(label),
                        selected: selected,
                        onSelected: (_) => _setRange(label),
                        selectedColor: const Color(0xFF0077FF),
                        labelStyle: TextStyle(
                          color: selected ? Colors.white : Colors.black87,
                          fontWeight: selected ? FontWeight.bold : FontWeight.normal,
                        ),
                      ),
                    );
                  }).toList()
                  ..add(Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ActionChip(
                      avatar: const Icon(Icons.calendar_today, size: 14),
                      label: Text(_rangeLabel == 'Custom'
                          ? '${_startDate.day}/${_startDate.month} – ${_endDate.day}/${_endDate.month}'
                          : 'Custom'),
                      onPressed: _pickCustomRange,
                    ),
                  )),
                ),
              ),
              TabBar(
                controller: _tabController,
                labelColor: const Color(0xFF0077FF),
                unselectedLabelColor: Colors.grey,
                tabs: const [
                  Tab(text: 'Overview'),
                  Tab(text: 'By Machine'),
                  Tab(text: 'Transactions'),
                ],
              ),
            ],
          ),
        ),

        Expanded(
          child: _loading
              ? const Center(child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    CircularProgressIndicator(),
                    SizedBox(height: 12),
                    Text('Fetching sales data...', style: TextStyle(color: Colors.grey)),
                  ],
                ))
              : _error != null
                  ? Center(child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        const Icon(Icons.error_outline, size: 48, color: Colors.red),
                        const SizedBox(height: 12),
                        Text(_error!, textAlign: TextAlign.center, style: const TextStyle(color: Colors.red)),
                        const SizedBox(height: 16),
                        ElevatedButton(onPressed: _load, child: const Text('Retry')),
                      ],
                    ))
                  : _data == null
                      ? const Center(child: Text('Select a date range'))
                      : TabBarView(
                          controller: _tabController,
                          children: [
                            _buildOverview(),
                            _buildByMachine(),
                            _buildTransactions(),
                          ],
                        ),
        ),
      ],
    );
  }

  Widget _buildOverview() {
    final d = _data!;
    final revenue = (d['total_revenue'] as num).toDouble();
    final totalTrx = d['total_transactions'] as int;
    final success = d['successful_transactions'] as int;
    final failed = d['failed_transactions'] as int;
    final refunds = (d['total_refunds'] as num).toDouble();
    final products = d['total_products_sold'] as int;

    return RefreshIndicator(
      onRefresh: _load,
      child: SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            // Revenue hero card
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                gradient: const LinearGradient(
                  colors: [Color(0xFF0077FF), Color(0xFF1565C0)],
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                ),
                borderRadius: BorderRadius.circular(16),
              ),
              child: Column(
                children: [
                  const Text('Total Revenue', style: TextStyle(color: Colors.white70, fontSize: 14)),
                  const SizedBox(height: 8),
                  Text('₹${revenue.toStringAsFixed(2)}',
                      style: const TextStyle(color: Colors.white, fontSize: 40, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 4),
                  Text(_rangeLabel, style: const TextStyle(color: Colors.white60, fontSize: 13)),
                ],
              ),
            ),
            const SizedBox(height: 12),

            // Stats grid
            Row(children: [
              _statCard('Transactions', '$totalTrx', Icons.receipt_long, Colors.blue),
              const SizedBox(width: 8),
              _statCard('Successful', '$success', Icons.check_circle_outline, Colors.green),
            ]),
            const SizedBox(height: 8),
            Row(children: [
              _statCard('Failed', '$failed', Icons.cancel_outlined, Colors.red),
              const SizedBox(width: 8),
              _statCard('Products Sold', '$products', Icons.shopping_bag_outlined, Colors.purple),
            ]),
            const SizedBox(height: 8),
            Row(children: [
              _statCard('Total Refunds', '₹${refunds.toStringAsFixed(2)}', Icons.undo, Colors.orange),
              const SizedBox(width: 8),
              _statCard('Avg Txn Value',
                  success > 0 ? '₹${(revenue / success).toStringAsFixed(2)}' : '₹0',
                  Icons.trending_up, Colors.teal),
            ]),
          ],
        ),
      ),
    );
  }

  Widget _statCard(String label, String value, IconData icon, Color color) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(12),
          boxShadow: [BoxShadow(color: Colors.black.withOpacity(0.06), blurRadius: 8, offset: const Offset(0, 2))],
        ),
        child: Row(
          children: [
            Container(
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(color: color.withOpacity(0.12), borderRadius: BorderRadius.circular(8)),
              child: Icon(icon, color: color, size: 20),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(value, style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: color)),
                  Text(label, style: const TextStyle(fontSize: 11, color: Colors.grey)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildByMachine() {
    final machines = (_data!['by_machine'] as List).cast<Map<String, dynamic>>();
    if (machines.isEmpty) return const Center(child: Text('No machine data', style: TextStyle(color: Colors.grey)));

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.builder(
        padding: const EdgeInsets.all(12),
        itemCount: machines.length,
        itemBuilder: (ctx, i) {
          final m = machines[i];
          final revenue = (m['total_revenue'] as num).toDouble();
          final maxRevenue = (machines.first['total_revenue'] as num).toDouble();
          final pct = maxRevenue > 0 ? revenue / maxRevenue : 0.0;
          return Card(
            margin: const EdgeInsets.only(bottom: 8),
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(
                          color: const Color(0xFF0077FF).withOpacity(0.1),
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Text('#${i + 1}', style: const TextStyle(color: Color(0xFF0077FF), fontWeight: FontWeight.bold, fontSize: 12)),
                      ),
                      const SizedBox(width: 8),
                      Expanded(child: Text(m['machine_display_id'], style: const TextStyle(fontWeight: FontWeight.bold))),
                      Text('₹${revenue.toStringAsFixed(2)}',
                          style: const TextStyle(fontWeight: FontWeight.bold, color: Color(0xFF0077FF), fontSize: 16)),
                    ],
                  ),
                  const SizedBox(height: 8),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(4),
                    child: LinearProgressIndicator(
                      value: pct,
                      backgroundColor: Colors.grey.shade200,
                      valueColor: const AlwaysStoppedAnimation(Color(0xFF0077FF)),
                      minHeight: 6,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Row(children: [
                    _miniStat(Icons.check_circle_outline, '${m['successful_transactions']} success', Colors.green),
                    const SizedBox(width: 12),
                    _miniStat(Icons.cancel_outlined, '${m['failed_transactions']} failed', Colors.red),
                    const SizedBox(width: 12),
                    _miniStat(Icons.shopping_bag_outlined, '${m['total_products_sold']} items', Colors.purple),
                  ]),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  Widget _miniStat(IconData icon, String text, Color color) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 13, color: color),
        const SizedBox(width: 3),
        Text(text, style: TextStyle(fontSize: 11, color: color)),
      ],
    );
  }

  Widget _buildTransactions() {
    final trxs = (_data!['transactions'] as List).cast<Map<String, dynamic>>();
    if (trxs.isEmpty) return const Center(child: Text('No transactions', style: TextStyle(color: Colors.grey)));

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.builder(
        padding: const EdgeInsets.all(12),
        itemCount: trxs.length,
        itemBuilder: (ctx, i) {
          final t = trxs[i];
          final success = t['status'] == 'SUCCESS';
          final amount = (t['amount'] as num).toDouble();
          final dt = DateTime.fromMillisecondsSinceEpoch(t['transaction_time'] as int);
          final timeStr = '${dt.day}/${dt.month} ${dt.hour.toString().padLeft(2,'0')}:${dt.minute.toString().padLeft(2,'0')}';
          final products = (t['products'] as List).cast<Map<String, dynamic>>();

          return Card(
            margin: const EdgeInsets.only(bottom: 8),
            child: ExpansionTile(
              leading: Container(
                width: 36, height: 36,
                decoration: BoxDecoration(
                  color: (success ? Colors.green : Colors.red).withOpacity(0.12),
                  shape: BoxShape.circle,
                ),
                child: Icon(success ? Icons.check : Icons.close,
                    color: success ? Colors.green : Colors.red, size: 18),
              ),
              title: Row(children: [
                Expanded(child: Text(t['machine_display_id'],
                    style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13))),
                Text('₹${amount.toStringAsFixed(2)}',
                    style: TextStyle(
                        fontWeight: FontWeight.bold,
                        color: success ? Colors.green : Colors.red)),
              ]),
              subtitle: Text('$timeStr  •  ${t['payment_method']}',
                  style: const TextStyle(fontSize: 11, color: Colors.grey)),
              children: products.isEmpty
                  ? [const Padding(
                      padding: EdgeInsets.all(12),
                      child: Text('No product details', style: TextStyle(color: Colors.grey)))]
                  : products.map((p) => ListTile(
                      dense: true,
                      leading: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(
                          color: Colors.purple.withOpacity(0.1),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Text(p['slot'], style: const TextStyle(fontSize: 10, color: Colors.purple, fontWeight: FontWeight.bold)),
                      ),
                      title: Text(p['product_name'], style: const TextStyle(fontSize: 12)),
                      subtitle: Text(p['product_id'], style: const TextStyle(fontSize: 10, color: Colors.grey)),
                      trailing: Text('${p['qty']} × ₹${(p['amount'] as num).toStringAsFixed(2)}',
                          style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold)),
                    )).toList(),
            ),
          );
        },
      ),
    );
  }
}
