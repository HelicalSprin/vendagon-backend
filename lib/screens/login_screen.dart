import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import 'dashboard_screen.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _loading = false;
  bool _obscurePassword = true;
  bool _rememberMe = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadSavedCredentials();
  }

  Future<void> _loadSavedCredentials() async {
    final prefs = await SharedPreferences.getInstance();
    final remember = prefs.getBool('remember_me') ?? false;
    if (remember) {
      setState(() {
        _rememberMe = true;
        _usernameController.text = prefs.getString('saved_username') ?? '';
        _passwordController.text = prefs.getString('saved_password') ?? '';
      });
    }
  }

  Future<void> _login() async {
    final username = _usernameController.text.trim();
    final password = _passwordController.text.trim();
    if (username.isEmpty || password.isEmpty) {
      setState(() => _error = 'Please enter username and password');
      return;
    }
    setState(() { _loading = true; _error = null; });
    try {
      final token = await ApiService.login(username, password);
      final prefs = await SharedPreferences.getInstance();

      // Save token for auto-login
      await prefs.setString('auth_token', token);

      // Save or clear credentials based on Remember Me
      await prefs.setBool('remember_me', _rememberMe);
      if (_rememberMe) {
        await prefs.setString('saved_username', username);
        await prefs.setString('saved_password', password);
      } else {
        await prefs.remove('saved_username');
        await prefs.remove('saved_password');
      }

      if (!mounted) return;
      Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => DashboardScreen(token: token)));
    } catch (e) {
      setState(() => _error = e.toString().replaceAll('Exception: ', ''));
    } finally {
      setState(() => _loading = false);
    }
  }

  @override
  void dispose() {
    _usernameController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      body: Container(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [
              Color(0xFFFFFFFF),
              Color(0xFFFEFEFE),
              Color(0xFFFBFBFB),
              Color(0xFFFAFAFA),
              Color(0xFFF9F9F9),
            ],
          ),
        ),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Image.asset('assets/logo_full.png', width: 280),
                  const SizedBox(height: 44),

                  Card(
                    elevation: 4,
                    color: Colors.white,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          const Text('Sign In',
                              style: TextStyle(
                                fontSize: 22,
                                fontWeight: FontWeight.bold,
                                color: Color(0xFF0077FF),
                              )),
                          const SizedBox(height: 20),

                          // Username with autofill hint
                          TextField(
                            controller: _usernameController,
                            autofillHints: const [AutofillHints.username],
                            decoration: InputDecoration(
                              labelText: 'Username',
                              filled: true,
                              fillColor: Colors.white,
                              prefixIcon: const Icon(Icons.person_outline),
                              border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
                            ),
                            textInputAction: TextInputAction.next,
                          ),
                          const SizedBox(height: 16),

                          // Password with autofill hint
                          TextField(
                            controller: _passwordController,
                            obscureText: _obscurePassword,
                            autofillHints: const [AutofillHints.password],
                            decoration: InputDecoration(
                              labelText: 'Password',
                              filled: true,
                              fillColor: Colors.white,
                              prefixIcon: const Icon(Icons.lock_outline),
                              suffixIcon: IconButton(
                                icon: Icon(_obscurePassword ? Icons.visibility_off : Icons.visibility),
                                onPressed: () => setState(() => _obscurePassword = !_obscurePassword),
                              ),
                              border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
                            ),
                            textInputAction: TextInputAction.done,
                            onSubmitted: (_) => _login(),
                          ),
                          const SizedBox(height: 8),

                          // Remember Me toggle
                          Row(
                            children: [
                              Transform.scale(
                                scale: 0.9,
                                child: Switch(
                                  value: _rememberMe,
                                  activeColor: const Color(0xFF0077FF),
                                  onChanged: (val) => setState(() => _rememberMe = val),
                                ),
                              ),
                              const Text('Remember me',
                                  style: TextStyle(fontSize: 13, color: Colors.black87)),
                              const Spacer(),
                              if (_rememberMe)
                                const Row(
                                  children: [
                                    Icon(Icons.check_circle, color: Color(0xFF0077FF), size: 14),
                                    SizedBox(width: 4),
                                    Text('Credentials saved',
                                        style: TextStyle(fontSize: 11, color: Color(0xFF0077FF))),
                                  ],
                                ),
                            ],
                          ),

                          if (_error != null) ...[
                            const SizedBox(height: 8),
                            Container(
                              padding: const EdgeInsets.all(12),
                              decoration: BoxDecoration(
                                color: Colors.red.shade50,
                                borderRadius: BorderRadius.circular(8),
                                border: Border.all(color: Colors.red.shade200),
                              ),
                              child: Row(children: [
                                const Icon(Icons.error_outline, color: Colors.red, size: 18),
                                const SizedBox(width: 8),
                                Expanded(child: Text(_error!, style: const TextStyle(color: Colors.red))),
                              ]),
                            ),
                          ],
                          const SizedBox(height: 16),

                          SizedBox(
                            height: 50,
                            child: ElevatedButton(
                              onPressed: _loading ? null : _login,
                              style: ElevatedButton.styleFrom(
                                backgroundColor: const Color(0xFF0077FF),
                                foregroundColor: Colors.white,
                                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                              ),
                              child: _loading
                                  ? const SizedBox(width: 24, height: 24,
                                      child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                                  : const Text('Sign In',
                                      style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
