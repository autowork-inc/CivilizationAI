import { Ionicons } from '@expo/vector-icons';
import { Link } from 'expo-router';
import { useRef, useState } from 'react';
import { Pressable, Text, TextInput, View } from 'react-native';

import { getErrorMessage } from '@/api/apiClient';
import { AuthHeader } from '@/components/AuthHeader';
import { Button } from '@/components/Button';
import { Screen } from '@/components/Screen';
import { TextField } from '@/components/TextField';
import { useAuth } from '@/context/AuthContext';

const PASSWORD_MIN = 8; // same as the backend

export default function RegisterScreen() {
  const { register } = useAuth();
  const passwordRef = useRef<TextInput>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!email.trim()) {
      setError('Введите почту.');
      return;
    }
    if (password.length < PASSWORD_MIN) {
      setError(`Пароль должен быть не короче ${PASSWORD_MIN} символов.`);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await register(email, password); // on success the root layout swaps (auth) for (app)
    } catch (e) {
      setError(getErrorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Screen>
      <AuthHeader title="Создать аккаунт" subtitle="Почта и пароль — этого достаточно, чтобы начать." />

      <TextField
        label="Email"
        value={email}
        onChangeText={setEmail}
        placeholder="you@example.com"
        keyboardType="email-address"
        textContentType="emailAddress"
        autoComplete="email"
        returnKeyType="next"
        blurOnSubmit={false}
        onSubmitEditing={() => passwordRef.current?.focus()}
      />
      <TextField
        ref={passwordRef}
        label="Пароль"
        value={password}
        onChangeText={setPassword}
        placeholder={`Не короче ${PASSWORD_MIN} символов`}
        secureTextEntry
        textContentType="newPassword"
        autoComplete="new-password"
        returnKeyType="go"
        onSubmitEditing={submit}
      />

      {error ? (
        <View className="mb-4 flex-row items-center rounded-2xl border border-red-500/30 bg-red-500/10 px-4 py-3">
          <Ionicons name="alert-circle" size={18} color="#f87171" />
          <Text className="ml-2 flex-1 text-sm text-red-300">{error}</Text>
        </View>
      ) : null}

      <View className="mt-2">
        <Button title="Создать аккаунт" onPress={submit} loading={busy} />
      </View>

      <Link href="/login" asChild>
        <Pressable className="mt-6 items-center py-2" hitSlop={8}>
          <Text className="text-base text-slate-400">
            Уже есть аккаунт? <Text className="font-semibold text-blue-400">Войти</Text>
          </Text>
        </Pressable>
      </Link>
    </Screen>
  );
}
