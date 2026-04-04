import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import 'status_screen.dart';
import 'problems_screen.dart';
import 'stock_screen.dart';
import 'sales_screen.dart';
import 'export_screen.dart';
import 'login_screen.dart';

class DashboardScreen extends StatefulWidget {
  final String token;
  const DashboardScreen({super.key, required this.token});
  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  int _currentIndex = 0;
  late ApiService _api;
  DateTime _lastRefreshed = DateTime.now();

  @override
  void initState() {
    super.initState();
    _api = ApiService(token: widget.token);
  }

  void updateRefreshTime() {
    setState(() => _lastRefreshed = DateTime.now());
  }

  void _logout() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('auth_token');
    if (!mounted) return;
    Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const LoginScreen()));
  }

  String _formattedTime() {
    final h = _lastRefreshed.hour.toString().padLeft(2, '0');
    final m = _lastRefreshed.minute.toString().padLeft(2, '0');
    return '$h:$m';
  }

  @override
  Widget build(BuildContext context) {
    final titles = ['Dashboard', 'Problems', 'Stock', 'Sales', 'Export'];

    final screens = [
      StatusScreen(api: _api, onRefresh: updateRefreshTime),
      ProblemsScreen(api: _api),
      StockScreen(api: _api),
      SalesScreen(api: _api),
      ExportScreen(api: _api),
    ];

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF1565C0),
        title: Row(
          children: [
            Image.asset('assets/vendagon_icon.webp', height: 28),
            const SizedBox(width: 8),
            Text(titles[_currentIndex],
                style: const TextStyle(fontWeight: FontWeight.bold, color: Colors.white)),
          ],
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 4),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                const Text('Updated', style: TextStyle(color: Colors.white54, fontSize: 10)),
                Text(_formattedTime(),
                    style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.bold)),
              ],
            ),
          ),
          IconButton(
            icon: const Icon(Icons.logout, color: Colors.white),
            tooltip: 'Logout',
            onPressed: () => showDialog(
              context: context,
              builder: (_) => AlertDialog(
                title: const Text('Sign Out'),
                content: const Text('Are you sure you want to sign out?'),
                actions: [
                  TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
                  TextButton(
                      onPressed: _logout,
                      child: const Text('Sign Out', style: TextStyle(color: Colors.red))),
                ],
              ),
            ),
          ),
        ],
      ),
      body: screens[_currentIndex],
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (i) => setState(() => _currentIndex = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.dashboard_outlined), selectedIcon: Icon(Icons.dashboard), label: 'Status'),
          NavigationDestination(icon: Icon(Icons.warning_amber_outlined), selectedIcon: Icon(Icons.warning_amber), label: 'Problems'),
          NavigationDestination(icon: Icon(Icons.bar_chart_outlined), selectedIcon: Icon(Icons.bar_chart), label: 'Stock'),
          NavigationDestination(icon: Icon(Icons.attach_money_outlined), selectedIcon: Icon(Icons.attach_money), label: 'Sales'),
          NavigationDestination(icon: Icon(Icons.download_outlined), selectedIcon: Icon(Icons.download), label: 'Export'),
        ],
      ),
    );
  }
}
