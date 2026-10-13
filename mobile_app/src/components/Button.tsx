import { ActivityIndicator, Pressable, Text } from 'react-native';

type Props = {
  title: string;
  onPress: () => void;
  loading?: boolean;
  disabled?: boolean;
  variant?: 'primary' | 'secondary';
};

export function Button({ title, onPress, loading = false, disabled = false, variant = 'primary' }: Props) {
  const inactive = disabled || loading;
  const primary = variant === 'primary';
  return (
    <Pressable
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityState={{ disabled: inactive, busy: loading }}
      className={`h-14 w-full items-center justify-center rounded-2xl active:scale-[0.98] ${
        primary ? 'bg-blue-600 active:bg-blue-700' : 'border border-slate-700 bg-slate-800 active:bg-slate-700'
      } ${inactive ? 'opacity-60' : ''}`}>
      {loading ? <ActivityIndicator color="#ffffff" /> : <Text className="text-base font-semibold text-white">{title}</Text>}
    </Pressable>
  );
}
